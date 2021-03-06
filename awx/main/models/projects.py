# Copyright (c) 2015 Ansible, Inc.
# All Rights Reserved.

# Python
import datetime
import os
import urlparse

# Django
from django.conf import settings
from django.db import models
from django.utils.translation import ugettext_lazy as _
from django.utils.encoding import smart_str, smart_text
from django.utils.text import slugify
from django.core.exceptions import ValidationError
from django.utils.timezone import now, make_aware, get_default_timezone

import six

# AWX
from awx.api.versioning import reverse
from awx.main.models.base import * # noqa
from awx.main.models.events import ProjectUpdateEvent
from awx.main.models.notifications import (
    NotificationTemplate,
    JobNotificationMixin,
)
from awx.main.models.unified_jobs import (
    UnifiedJob,
    UnifiedJobTemplate,
    ACTIVE_STATES,
)
from awx.main.models.mixins import (
    ResourceMixin,
    TaskManagerProjectUpdateMixin,
    CustomVirtualEnvMixin,
    RelatedJobsMixin
)
from awx.main.utils import update_scm_url
from awx.main.utils.ansible import skip_directory, could_be_inventory, could_be_playbook
from awx.main.fields import ImplicitRoleField
from awx.main.models.rbac import (
    ROLE_SINGLETON_SYSTEM_ADMINISTRATOR,
    ROLE_SINGLETON_SYSTEM_AUDITOR,
)
from awx.main.fields import JSONField

__all__ = ['Project', 'ProjectUpdate']


class ProjectOptions(models.Model):

    SCM_TYPE_CHOICES = [
        ('', _('Manual')),
        ('git', _('Git')),
        ('hg', _('Mercurial')),
        ('svn', _('Subversion')),
        ('insights', _('Red Hat Insights')),
    ]

    class Meta:
        abstract = True

    # Project files must be available on the server in folders directly
    # beneath the path specified by settings.PROJECTS_ROOT.  There is no way
    # via the API to upload/update a project or its playbooks; this must be
    # done by other means for now.

    @classmethod
    def get_local_path_choices(cls):
        if os.path.exists(settings.PROJECTS_ROOT):
            paths = [x.decode('utf-8') for x in os.listdir(settings.PROJECTS_ROOT)
                     if (os.path.isdir(os.path.join(settings.PROJECTS_ROOT, x)) and
                         not x.startswith('.') and not x.startswith('_'))]
            qs = Project.objects
            used_paths = qs.values_list('local_path', flat=True)
            return [x for x in paths if x not in used_paths]
        else:
            return []

    local_path = models.CharField(
        max_length=1024,
        blank=True,
        help_text=_('Local path (relative to PROJECTS_ROOT) containing '
                    'playbooks and related files for this project.')
    )

    scm_type = models.CharField(
        max_length=8,
        choices=SCM_TYPE_CHOICES,
        blank=True,
        default='',
        verbose_name=_('SCM Type'),
        help_text=_("Specifies the source control system used to store the project."),
    )
    scm_url = models.CharField(
        max_length=1024,
        blank=True,
        default='',
        verbose_name=_('SCM URL'),
        help_text=_("The location where the project is stored."),
    )
    scm_branch = models.CharField(
        max_length=256,
        blank=True,
        default='',
        verbose_name=_('SCM Branch'),
        help_text=_('Specific branch, tag or commit to checkout.'),
    )
    scm_clean = models.BooleanField(
        default=False,
        help_text=_('Discard any local changes before syncing the project.'),
    )
    scm_delete_on_update = models.BooleanField(
        default=False,
        help_text=_('Delete the project before syncing.'),
    )
    credential = models.ForeignKey(
        'Credential',
        related_name='%(class)ss',
        blank=True,
        null=True,
        default=None,
        on_delete=models.SET_NULL,
    )
    timeout = models.IntegerField(
        blank=True,
        default=0,
        help_text=_("The amount of time (in seconds) to run before the task is canceled."),
    )

    def clean_scm_type(self):
        return self.scm_type or ''

    def clean_scm_url(self):
        if self.scm_type == 'insights':
            self.scm_url = settings.INSIGHTS_URL_BASE
        scm_url = six.text_type(self.scm_url or '')
        if not self.scm_type:
            return ''
        try:
            scm_url = update_scm_url(self.scm_type, scm_url,
                                     check_special_cases=False)
        except ValueError as e:
            raise ValidationError((e.args or (_('Invalid SCM URL.'),))[0])
        scm_url_parts = urlparse.urlsplit(scm_url)
        if self.scm_type and not any(scm_url_parts):
            raise ValidationError(_('SCM URL is required.'))
        return six.text_type(self.scm_url or '')

    def clean_credential(self):
        if not self.scm_type:
            return None
        cred = self.credential
        if not cred and self.scm_type == 'insights':
            raise ValidationError(_("Insights Credential is required for an Insights Project."))
        elif cred:
            if self.scm_type == 'insights':
                if cred.kind != 'insights':
                    raise ValidationError(_("Credential kind must be 'insights'."))
            elif cred.kind != 'scm':
                raise ValidationError(_("Credential kind must be 'scm'."))
            try:
                if self.scm_type == 'insights':
                    self.scm_url = settings.INSIGHTS_URL_BASE
                scm_url = update_scm_url(self.scm_type, self.scm_url,
                                         check_special_cases=False)
                scm_url_parts = urlparse.urlsplit(scm_url)
                # Prefer the username/password in the URL, if provided.
                scm_username = scm_url_parts.username or cred.username or ''
                if scm_url_parts.password or cred.password:
                    scm_password = '********'
                else:
                    scm_password = ''
                try:
                    update_scm_url(self.scm_type, self.scm_url, scm_username,
                                   scm_password)
                except ValueError as e:
                    raise ValidationError((e.args or (_('Invalid credential.'),))[0])
            except ValueError:
                pass
        return cred

    def get_project_path(self, check_if_exists=True):
        local_path = os.path.basename(self.local_path)
        if local_path and not local_path.startswith('.'):
            proj_path = os.path.join(settings.PROJECTS_ROOT, local_path)
            if not check_if_exists or os.path.exists(smart_str(proj_path)):
                return proj_path

    @property
    def playbooks(self):
        results = []
        project_path = self.get_project_path()
        if project_path:
            for dirpath, dirnames, filenames in os.walk(smart_str(project_path)):
                if skip_directory(dirpath):
                    continue
                for filename in filenames:
                    playbook = could_be_playbook(project_path, dirpath, filename)
                    if playbook is not None:
                        results.append(smart_text(playbook))
        return sorted(results, key=lambda x: smart_str(x).lower())


    @property
    def inventories(self):
        results = []
        project_path = self.get_project_path()
        if project_path:
            # Cap the number of results, because it could include lots
            max_inventory_listing = 50
            for dirpath, dirnames, filenames in os.walk(smart_str(project_path)):
                if skip_directory(dirpath):
                    continue
                for filename in filenames:
                    inv_path = could_be_inventory(project_path, dirpath, filename)
                    if inv_path is not None:
                        results.append(smart_text(inv_path))
                        if len(results) > max_inventory_listing:
                            break
                if len(results) > max_inventory_listing:
                    break
        return sorted(results, key=lambda x: smart_str(x).lower())

    def get_lock_file(self):
        '''
        We want the project path in name only, we don't care if it exists or
        not. This method will just append .lock onto the full directory path.
        '''
        proj_path = self.get_project_path(check_if_exists=False)
        if not proj_path:
            return None
        return proj_path + '.lock'


class Project(UnifiedJobTemplate, ProjectOptions, ResourceMixin, CustomVirtualEnvMixin, RelatedJobsMixin):
    '''
    A project represents a playbook git repo that can access a set of inventories
    '''

    SOFT_UNIQUE_TOGETHER = [('polymorphic_ctype', 'name', 'organization')]
    FIELDS_TO_PRESERVE_AT_COPY = ['labels', 'instance_groups', 'credentials']
    FIELDS_TO_DISCARD_AT_COPY = ['local_path']

    class Meta:
        app_label = 'main'
        ordering = ('id',)

    organization = models.ForeignKey(
        'Organization',
        blank=True,
        null=True,
        on_delete=models.CASCADE,
        related_name='projects',
    )
    scm_delete_on_next_update = models.BooleanField(
        default=False,
        editable=False,
    )
    scm_update_on_launch = models.BooleanField(
        default=False,
        help_text=_('Update the project when a job is launched that uses the project.'),
    )
    scm_update_cache_timeout = models.PositiveIntegerField(
        default=0,
        blank=True,
        help_text=_('The number of seconds after the last project update ran that a new'
                    'project update will be launched as a job dependency.'),
    )

    scm_revision = models.CharField(
        max_length=1024,
        blank=True,
        default='',
        editable=False,
        verbose_name=_('SCM Revision'),
        help_text=_('The last revision fetched by a project update'),
    )

    playbook_files = JSONField(
        blank=True,
        default=[],
        editable=False,
        verbose_name=_('Playbook Files'),
        help_text=_('List of playbooks found in the project'),
    )

    inventory_files = JSONField(
        blank=True,
        default=[],
        editable=False,
        verbose_name=_('Inventory Files'),
        help_text=_('Suggested list of content that could be Ansible inventory in the project'),
    )

    admin_role = ImplicitRoleField(parent_role=[
        'organization.project_admin_role',
        'singleton:' + ROLE_SINGLETON_SYSTEM_ADMINISTRATOR,
    ])

    use_role = ImplicitRoleField(
        parent_role='admin_role',
    )

    update_role = ImplicitRoleField(
        parent_role='admin_role',
    )

    read_role = ImplicitRoleField(parent_role=[
        'organization.auditor_role',
        'singleton:' + ROLE_SINGLETON_SYSTEM_AUDITOR,
        'use_role',
        'update_role',
    ])

    @classmethod
    def _get_unified_job_class(cls):
        return ProjectUpdate

    @classmethod
    def _get_unified_job_field_names(cls):
        return set(f.name for f in ProjectOptions._meta.fields) | set(
            ['name', 'description', 'schedule']
        )

    def save(self, *args, **kwargs):
        new_instance = not bool(self.pk)
        # If update_fields has been specified, add our field names to it,
        # if it hasn't been specified, then we're just doing a normal save.
        update_fields = kwargs.get('update_fields', [])
        skip_update = bool(kwargs.pop('skip_update', False))
        # Check if scm_type or scm_url changes.
        if self.pk:
            project_before = self.__class__.objects.get(pk=self.pk)
            if project_before.scm_type != self.scm_type or project_before.scm_url != self.scm_url:
                self.scm_delete_on_next_update = True
                if 'scm_delete_on_next_update' not in update_fields:
                    update_fields.append('scm_delete_on_next_update')
        # Create auto-generated local path if project uses SCM.
        if self.pk and self.scm_type and not self.local_path.startswith('_'):
            slug_name = slugify(six.text_type(self.name)).replace(u'-', u'_')
            self.local_path = u'_%d__%s' % (int(self.pk), slug_name)
            if 'local_path' not in update_fields:
                update_fields.append('local_path')
        # Do the actual save.
        super(Project, self).save(*args, **kwargs)
        if new_instance:
            update_fields=[]
            # Generate local_path for SCM after initial save (so we have a PK).
            if self.scm_type and not self.local_path.startswith('_'):
                update_fields.append('local_path')
            if update_fields:
                from awx.main.signals import disable_activity_stream
                with disable_activity_stream():
                    self.save(update_fields=update_fields)
        # If we just created a new project with SCM, start the initial update.
        if new_instance and self.scm_type and not skip_update:
            self.update()

    def _get_current_status(self):
        if self.scm_type:
            if self.current_job and self.current_job.status:
                return self.current_job.status
            elif not self.last_job:
                return 'never updated'
            # inherit the child job status on failure
            elif self.last_job_failed:
                return self.last_job.status
            # Return the successful status
            else:
                return self.last_job.status
        elif not self.get_project_path():
            return 'missing'
        else:
            return 'ok'

    def _get_last_job_run(self):
        if self.scm_type and self.last_job:
            return self.last_job.finished
        else:
            project_path = self.get_project_path()
            if project_path:
                try:
                    mtime = os.path.getmtime(smart_str(project_path))
                    dt = datetime.datetime.fromtimestamp(mtime)
                    return make_aware(dt, get_default_timezone())
                except os.error:
                    pass

    def _can_update(self):
        return bool(self.scm_type)

    def _update_unified_job_kwargs(self, create_kwargs, kwargs):
        '''
        :param create_kwargs: key-worded arguments to be updated and later used for creating unified job.
        :type create_kwargs: dict
        :param kwargs: request parameters used to override unified job template fields with runtime values.
        :type kwargs: dict
        :return: modified create_kwargs.
        :rtype: dict
        '''
        if self.scm_delete_on_next_update:
            create_kwargs['scm_delete_on_update'] = True
        return create_kwargs

    def create_project_update(self, **kwargs):
        return self.create_unified_job(**kwargs)

    @property
    def cache_timeout_blocked(self):
        if not self.last_job_run:
            return False
        if (self.last_job_run + datetime.timedelta(seconds=self.scm_update_cache_timeout)) > now():
            return True
        return False

    @property
    def needs_update_on_launch(self):
        if self.scm_type and self.scm_update_on_launch:
            if not self.last_job_run:
                return True
            if (self.last_job_run + datetime.timedelta(seconds=self.scm_update_cache_timeout)) <= now():
                return True
        return False

    @property
    def notification_templates(self):
        base_notification_templates = NotificationTemplate.objects
        error_notification_templates = list(base_notification_templates
                                            .filter(unifiedjobtemplate_notification_templates_for_errors=self))
        success_notification_templates = list(base_notification_templates
                                              .filter(unifiedjobtemplate_notification_templates_for_success=self))
        any_notification_templates = list(base_notification_templates
                                          .filter(unifiedjobtemplate_notification_templates_for_any=self))
        # Get Organization NotificationTemplates
        if self.organization is not None:
            error_notification_templates = set(error_notification_templates +
                                               list(base_notification_templates
                                                    .filter(organization_notification_templates_for_errors=self.organization)))
            success_notification_templates = set(success_notification_templates +
                                                 list(base_notification_templates
                                                      .filter(organization_notification_templates_for_success=self.organization)))
            any_notification_templates = set(any_notification_templates +
                                             list(base_notification_templates
                                                  .filter(organization_notification_templates_for_any=self.organization)))
        return dict(error=list(error_notification_templates),
                    success=list(success_notification_templates),
                    any=list(any_notification_templates))

    def get_absolute_url(self, request=None):
        return reverse('api:project_detail', kwargs={'pk': self.pk}, request=request)

    '''
    RelatedJobsMixin
    '''
    def _get_active_jobs(self):
        return UnifiedJob.objects.non_polymorphic().filter(
            models.Q(status__in=ACTIVE_STATES) &
            (
                models.Q(Job___project=self) |
                models.Q(ProjectUpdate___project=self)
            )
        )



class ProjectUpdate(UnifiedJob, ProjectOptions, JobNotificationMixin, TaskManagerProjectUpdateMixin):
    '''
    Internal job for tracking project updates from SCM.
    '''

    class Meta:
        app_label = 'main'

    project = models.ForeignKey(
        'Project',
        related_name='project_updates',
        on_delete=models.CASCADE,
        editable=False,
    )

    job_type = models.CharField(
        max_length=64,
        choices=PROJECT_UPDATE_JOB_TYPE_CHOICES,
        default='check',
    )

    @classmethod
    def _get_parent_field_name(cls):
        return 'project'

    @classmethod
    def _get_task_class(cls):
        from awx.main.tasks import RunProjectUpdate
        return RunProjectUpdate

    def _global_timeout_setting(self):
        return 'DEFAULT_PROJECT_UPDATE_TIMEOUT'

    def is_blocked_by(self, obj):
        if type(obj) == ProjectUpdate:
            if self.project == obj.project:
                return True
        if type(obj) == Job:
            if self.project == obj.project:
                return True
        return False

    def websocket_emit_data(self):
        websocket_data = super(ProjectUpdate, self).websocket_emit_data()
        websocket_data.update(dict(project_id=self.project.id))
        return websocket_data

    @property
    def event_class(self):
        return ProjectUpdateEvent

    @property
    def task_impact(self):
        return 0 if self.job_type == 'run' else 1

    @property
    def result_stdout(self):
        return self._result_stdout_raw(redact_sensitive=True, escape_ascii=True)

    @property
    def result_stdout_raw(self):
        return self._result_stdout_raw(redact_sensitive=True)

    def result_stdout_raw_limited(self, start_line=0, end_line=None, redact_sensitive=True):
        return self._result_stdout_raw_limited(start_line, end_line, redact_sensitive=redact_sensitive)

    def result_stdout_limited(self, start_line=0, end_line=None, redact_sensitive=True):
        return self._result_stdout_raw_limited(start_line, end_line, redact_sensitive=redact_sensitive, escape_ascii=True)

    def get_absolute_url(self, request=None):
        return reverse('api:project_update_detail', kwargs={'pk': self.pk}, request=request)

    def get_ui_url(self):
        return urlparse.urljoin(settings.TOWER_URL_BASE, "/#/scm_update/{}".format(self.pk))

    def _update_parent_instance(self):
        parent_instance = self._get_parent_instance()
        if parent_instance and self.job_type == 'check':
            update_fields = self._update_parent_instance_no_save(parent_instance)
            if self.status in ('successful', 'failed', 'error', 'canceled'):
                if not self.failed and parent_instance.scm_delete_on_next_update:
                    parent_instance.scm_delete_on_next_update = False
                    if 'scm_delete_on_next_update' not in update_fields:
                        update_fields.append('scm_delete_on_next_update')
            parent_instance.save(update_fields=update_fields)

    def cancel(self, job_explanation=None, is_chain=False):
        res = super(ProjectUpdate, self).cancel(job_explanation=job_explanation, is_chain=is_chain)
        if res and self.launch_type != 'sync':
            for inv_src in self.scm_inventory_updates.filter(status='running'):
                inv_src.cancel(job_explanation=six.text_type(
                    'Source project update `{}` was canceled.').format(self.name))
        return res

    '''
    JobNotificationMixin
    '''
    def get_notification_templates(self):
        return self.project.notification_templates

    def get_notification_friendly_name(self):
        return "Project Update"

    @property
    def preferred_instance_groups(self):
        if self.project is not None and self.project.organization is not None:
            organization_groups = [x for x in self.project.organization.instance_groups.all()]
        else:
            organization_groups = []
        template_groups = [x for x in super(ProjectUpdate, self).preferred_instance_groups]
        selected_groups = template_groups + organization_groups
        if not selected_groups:
            return self.global_instance_groups
        return selected_groups


