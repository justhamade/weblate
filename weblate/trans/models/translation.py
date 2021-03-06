# -*- coding: utf-8 -*-
#
# Copyright © 2012 - 2014 Michal Čihař <michal@cihar.com>
#
# This file is part of Weblate <http://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

from django.db import models
from django.contrib.auth.models import User
from django.db.models import Q, Sum, Count
from django.utils.translation import ugettext as _
from django.utils.safestring import mark_safe
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.utils import timezone
from django.core.urlresolvers import reverse
import os
import git
import traceback
import ConfigParser
from translate.storage import poheader
from datetime import datetime, timedelta

import weblate
from weblate import appsettings
from weblate.lang.models import Language
from weblate.trans.formats import AutoFormat
from weblate.trans.checks import CHECKS
from weblate.trans.models.unit import Unit
from weblate.trans.models.unitdata import Check, Suggestion, Comment
from weblate.trans.util import (
    get_site_url, sleep_while_git_locked, translation_percent, split_plural,
)
from weblate.accounts.avatar import get_user_display
from weblate.trans.mixins import URLMixin, PercentMixin
from weblate.trans.boolean_sum import BooleanSum
from weblate.accounts.models import notify_new_string
from weblate.trans.models.changes import Change


class TranslationManager(models.Manager):
    def check_sync(self, subproject, code, path, force=False, request=None):
        '''
        Parses translation meta info and creates/updates translation object.
        '''
        lang = Language.objects.auto_get_or_create(code=code)
        translation, dummy = self.get_or_create(
            language=lang,
            language_code=code,
            subproject=subproject
        )
        if translation.filename != path:
            force = True
            translation.filename = path
        translation.check_sync(force, request=request)

        return translation

    def enabled(self):
        '''
        Filters enabled translations.
        '''
        return self.filter(enabled=True).select_related()

    def get_percents(self, project=None, subproject=None, language=None):
        '''
        Returns tuple consting of status percents -
        (translated, fuzzy, failing checks)
        '''
        # Filter translations
        translations = self
        if project is not None:
            translations = translations.filter(subproject__project=project)
        if subproject is not None:
            translations = translations.filter(subproject=subproject)
        if language is not None:
            translations = translations.filter(language=language)

        # Aggregate
        translations = translations.aggregate(
            Sum('translated'),
            Sum('fuzzy'),
            Sum('failing_checks'),
            Sum('total'),
        )

        total = translations['total__sum']

        # Catch no translations (division by zero)
        if total == 0 or total is None:
            return (0, 0, 0)

        # Fetch values
        result = [
            translations['translated__sum'],
            translations['fuzzy__sum'],
            translations['failing_checks__sum'],
        ]
        # Calculate percent
        return tuple([translation_percent(value, total) for value in result])


class Translation(models.Model, URLMixin, PercentMixin):
    subproject = models.ForeignKey('SubProject')
    language = models.ForeignKey(Language)
    revision = models.CharField(max_length=100, default='', blank=True)
    filename = models.CharField(max_length=200)

    translated = models.IntegerField(default=0, db_index=True)
    fuzzy = models.IntegerField(default=0, db_index=True)
    total = models.IntegerField(default=0, db_index=True)
    translated_words = models.IntegerField(default=0)
    fuzzy_words = models.IntegerField(default=0)
    failing_checks_words = models.IntegerField(default=0)
    total_words = models.IntegerField(default=0)
    failing_checks = models.IntegerField(default=0, db_index=True)
    have_suggestion = models.IntegerField(default=0, db_index=True)

    enabled = models.BooleanField(default=True, db_index=True)

    language_code = models.CharField(max_length=20, default='')

    lock_user = models.ForeignKey(User, null=True, blank=True, default=None)
    lock_time = models.DateTimeField(default=datetime.now)

    commit_message = models.TextField(default='', blank=True)

    objects = TranslationManager()

    is_git_lockable = False

    class Meta(object):
        ordering = ['language__name']
        permissions = (
            ('upload_translation', "Can upload translation"),
            ('overwrite_translation', "Can overwrite with translation upload"),
            ('author_translation', "Can define author of translation upload"),
            ('commit_translation', "Can force commiting of translation"),
            ('update_translation', "Can update translation from"),
            ('push_translation', "Can push translations to remote"),
            ('reset_translation', "Can reset translations to match remote"),
            ('automatic_translation', "Can do automatic translation"),
            ('lock_translation', "Can lock whole translation project"),
            ('use_mt', "Can use machine translation"),
        )
        app_label = 'trans'

    def __init__(self, *args, **kwargs):
        '''
        Constructor to initialize some cache properties.
        '''
        super(Translation, self).__init__(*args, **kwargs)
        self._store = None

    def has_acl(self, user):
        '''
        Checks whether current user is allowed to access this
        subproject.
        '''
        return self.subproject.project.has_acl(user)

    def check_acl(self, request):
        '''
        Raises an error if user is not allowed to access this project.
        '''
        self.subproject.project.check_acl(request)

    def clean(self):
        '''
        Validates that filename exists and can be opened using
        translate-toolkit.
        '''
        if not os.path.exists(self.get_filename()):
            raise ValidationError(
                _(
                    'Filename %s not found in repository! To add new '
                    'translation, add language file into repository.'
                ) %
                self.filename
            )
        try:
            self.load_store()
        except ValueError:
            raise ValidationError(
                _('Format of %s could not be recognized.') %
                self.filename
            )
        except Exception as error:
            raise ValidationError(
                _('Failed to parse file %(file)s: %(error)s') % {
                    'file': self.filename,
                    'error': str(error)
                }
            )

    def _get_percents(self):
        '''
        Returns percentages of translation status.
        '''
        # No units?
        if self.total == 0:
            return (0, 0, 0)

        return (
            translation_percent(self.translated, self.total),
            translation_percent(self.fuzzy, self.total),
            translation_percent(self.failing_checks, self.total),
        )

    def get_words_percent(self):
        if self.total_words == 0:
            return 0
        return translation_percent(self.translated_words, self.total_words)

    @property
    def untranslated_words(self):
        return self.total_words - self.translated_words

    def get_lock_user_display(self):
        '''
        Returns formatted lock user.
        '''
        return get_user_display(self.lock_user)

    def get_lock_display(self):
        return mark_safe(
            _('This translation is locked by %(user)s!') % {
                'user': self.get_lock_user_display(),
            }
        )

    def is_locked(self, request=None, multi=False):
        '''
        Check whether the translation is locked and
        possibly emits messages if request object is
        provided.
        '''

        prj_lock = self.subproject.locked
        usr_lock, own_lock = self.is_user_locked(request, True)

        # Calculate return value
        if multi:
            return (prj_lock, usr_lock, own_lock)
        else:
            return prj_lock or usr_lock

    def is_user_locked(self, request=None, multi=False):
        '''
        Checks whether there is valid user lock on this translation.
        '''
        # Any user?
        if self.lock_user is None:
            result = (False, False)

        # Is lock still valid?
        elif self.lock_time < datetime.now():
            # Clear the lock
            self.create_lock(None)

            result = (False, False)

        # Is current user the one who has locked?
        elif request is not None and self.lock_user == request.user:
            result = (False, True)

        else:
            result = (True, False)

        if multi:
            return result
        else:
            return result[0]

    def create_lock(self, user, explicit=False):
        '''
        Creates lock on translation.
        '''
        is_new = self.lock_user is None
        self.lock_user = user

        # Clean timestamp on unlock
        if user is None:
            self.lock_time = datetime.now()
            self.save()
            return

        self.update_lock_time(explicit, is_new)

    def update_lock_time(self, explicit=False, is_new=True):
        '''
        Sets lock timestamp.
        '''
        if explicit:
            seconds = appsettings.LOCK_TIME
        else:
            seconds = appsettings.AUTO_LOCK_TIME

        new_lock_time = datetime.now() + timedelta(seconds=seconds)

        if is_new or new_lock_time > self.lock_time:
            self.lock_time = new_lock_time

        self.save()

    def update_lock(self, request):
        '''
        Updates lock timestamp.
        '''
        # Update timestamp
        if self.is_user_locked():
            self.update_lock_time()
            return

        # Auto lock if we should
        if appsettings.AUTO_LOCK:
            self.create_lock(request.user)
            return

    def get_non_translated(self):
        return self.total - self.translated

    def _reverse_url_name(self):
        '''
        Returns base name for URL reversing.
        '''
        return 'translation'

    def _reverse_url_kwargs(self):
        '''
        Returns kwargs for URL reversing.
        '''
        return {
            'project': self.subproject.project.slug,
            'subproject': self.subproject.slug,
            'lang': self.language.code
        }

    def get_widgets_url(self):
        '''
        Returns absolute URL for widgets.
        '''
        return get_site_url(
            '%s?lang=%s' % (
                reverse(
                    'widgets', kwargs={
                        'project': self.subproject.project.slug,
                    }
                ),
                self.language.code,
            )
        )

    def get_share_url(self):
        '''
        Returns absolute URL usable for sharing.
        '''
        return get_site_url(
            reverse(
                'engage-lang',
                kwargs={
                    'project': self.subproject.project.slug,
                    'lang': self.language.code
                }
            )
        )

    @models.permalink
    def get_translate_url(self):
        return ('translate', (), {
            'project': self.subproject.project.slug,
            'subproject': self.subproject.slug,
            'lang': self.language.code
        })

    def __unicode__(self):
        return '%s - %s' % (
            self.subproject.__unicode__(),
            _(self.language.name)
        )

    def get_filename(self):
        '''
        Returns absolute filename.
        '''
        return os.path.join(self.subproject.get_path(), self.filename)

    def load_store(self):
        '''
        Loads translate-toolkit storage from disk.
        '''
        return self.subproject.file_format_cls(
            self.get_filename(),
            self.subproject.template_store,
            language_code=self.language_code
        )

    def supports_language_pack(self):
        '''
        Checks whether we support language pack download.
        '''
        return self.subproject.file_format_cls.supports_language_pack()

    @property
    def store(self):
        '''
        Returns translate-toolkit storage object for a translation.
        '''
        if self._store is None:
            try:
                self._store = self.load_store()
            except Exception as exc:
                weblate.logger.warning(
                    'failed parsing store %s: %s',
                    self.__unicode__(),
                    str(exc)
                )
                self.subproject.notify_merge_failure(
                    str(exc),
                    ''.join(traceback.format_stack()),
                )
                raise
        return self._store

    def cleanup_deleted(self, deleted_contentsums):
        '''
        Removes stale checks/comments/suggestions for deleted units.
        '''
        for contentsum in deleted_contentsums:
            units = Unit.objects.filter(
                translation__language=self.language,
                translation__subproject__project=self.subproject.project,
                contentsum=contentsum
            )
            if units.exists():
                # There are other units as well, but some checks
                # (eg. consistency) needs update now
                for unit in units:
                    unit.run_checks()
                continue

            # Last unit referencing to these checks
            Check.objects.filter(
                project=self.subproject.project,
                language=self.language,
                contentsum=contentsum
            ).delete()
            # Delete suggestons referencing this unit
            Suggestion.objects.filter(
                project=self.subproject.project,
                language=self.language,
                contentsum=contentsum
            ).delete()
            # Delete translation comments referencing this unit
            Comment.objects.filter(
                project=self.subproject.project,
                language=self.language,
                contentsum=contentsum
            ).delete()
            # Check for other units with same source
            other_units = Unit.objects.filter(
                translation__subproject__project=self.subproject.project,
                contentsum=contentsum
            )
            if not other_units.exists():
                # Delete source comments as well if this was last reference
                Comment.objects.filter(
                    project=self.subproject.project,
                    language=None,
                    contentsum=contentsum
                ).delete()
                # Delete source checks as well if this was last reference
                Check.objects.filter(
                    project=self.subproject.project,
                    language=None,
                    contentsum=contentsum
                ).delete()

    def check_sync(self, force=False, request=None, change=None):
        '''
        Checks whether database is in sync with git and possibly does update.
        '''

        if change is None:
            change = Change.ACTION_UPDATE

        # Check if we're not already up to date
        if self.revision != self.get_git_blob_hash():
            weblate.logger.info(
                'processing %s in %s, revision has changed',
                self.filename,
                self.subproject.__unicode__()
            )
        elif force:
            weblate.logger.info(
                'processing %s in %s, check forced',
                self.filename,
                self.subproject.__unicode__()
            )
        else:
            return

        # List of created units (used for cleanup and duplicates detection)
        created_units = set()

        # Was there change?
        was_new = False
        # Position of current unit
        pos = 1

        for unit in self.store.all_units():
            if not unit.is_translatable():
                continue

            newunit, is_new = Unit.objects.update_from_unit(
                self, unit, pos
            )

            # Check if unit is new and untranslated
            was_new = (
                was_new
                or (is_new and not newunit.translated)
                or (
                    not newunit.translated
                    and newunit.translated != newunit.old_translated
                )
                or (newunit.fuzzy and newunit.fuzzy != newunit.old_fuzzy)
            )

            # Update position
            pos += 1

            # Check for possible duplicate units
            if newunit.id in created_units:
                weblate.logger.error(
                    'Duplicate string to translate in %s: %s (%s)',
                    self,
                    newunit,
                    repr(newunit.source)
                )

            # Store current unit ID
            created_units.add(newunit.id)

        # Following query can get huge, so we should find better way
        # to delete stale units, probably sort of garbage collection

        # We should also do cleanup on source stings tracking objects

        # Get lists of stale units to delete
        units_to_delete = self.unit_set.exclude(
            id__in=created_units
        )
        # We need to resolve this now as otherwise list will become empty after
        # delete
        deleted_contentsums = list(
            units_to_delete.values_list('contentsum', flat=True)
        )
        # Actually delete units
        units_to_delete.delete()

        # Cleanup checks for deleted units
        self.cleanup_deleted(deleted_contentsums)

        # Update revision and stats
        self.update_stats()

        # Cleanup checks cache if there were some deleted units
        if len(deleted_contentsums) > 0:
            self.invalidate_cache()

        # Store change entry
        if request is None:
            user = None
        else:
            user = request.user
        Change.objects.create(
            translation=self,
            action=change,
            user=user,
            author=user
        )

        # Notify subscribed users
        if was_new:
            notify_new_string(self)

    @property
    def git_repo(self):
        return self.subproject.git_repo

    def get_last_remote_commit(self):
        return self.subproject.get_last_remote_commit()

    def do_update(self, request=None):
        return self.subproject.do_update(request)

    def do_push(self, request=None):
        return self.subproject.do_push(request)

    def do_reset(self, request=None):
        return self.subproject.do_reset(request)

    def can_push(self):
        return self.subproject.can_push()

    def get_git_blob_hash(self):
        '''
        Returns current Git blob hash for file.
        '''
        tree = self.git_repo.tree()
        try:
            ret = tree[self.filename].hexsha
        except KeyError:
            # Try to resolve symlinks
            real_path = os.path.realpath(self.get_filename())
            project_path = os.path.realpath(self.subproject.get_path())

            if not real_path.startswith(project_path):
                raise ValueError('Too many symlinks or link outside tree')

            real_path = real_path[len(project_path):].lstrip('/')

            ret = tree[real_path].hexsha

        if self.subproject.has_template():
            ret += ','
            ret += tree[self.subproject.template].hexsha
        return ret

    def update_stats(self):
        '''
        Updates translation statistics.
        '''
        # Grab stats
        stats = self.unit_set.aggregate(
            Sum('num_words'),
            BooleanSum('fuzzy'),
            BooleanSum('translated'),
            BooleanSum('has_failing_check'),
            BooleanSum('has_suggestion'),
            Count('id'),
        )

        # Check if we have any units
        if stats['num_words__sum'] is None:
            self.total_words = 0
            self.total = 0
            self.fuzzy = 0
            self.translated = 0
            self.failing_checks = 0
            self.have_suggestion = 0
        else:
            self.total_words = stats['num_words__sum']
            self.total = stats['id__count']
            self.fuzzy = int(stats['fuzzy__sum'])
            self.translated = int(stats['translated__sum'])
            self.failing_checks = int(stats['has_failing_check__sum'])
            self.have_suggestion = int(stats['has_suggestion__sum'])

        # Count translated words
        self.translated_words = self.unit_set.filter(
            translated=True
        ).aggregate(
            Sum('num_words')
        )['num_words__sum']
        # Nothing matches filter
        if self.translated_words is None:
            self.translated_words = 0

        # Count fuzzy words
        self.fuzzy_words = self.unit_set.filter(
            fuzzy=True
        ).aggregate(
            Sum('num_words')
        )['num_words__sum']
        # Nothing matches filter
        if self.fuzzy_words is None:
            self.fuzzy_words = 0

        # Count words with failing checks
        self.failing_checks_words = self.unit_set.filter(
            has_failing_check=True
        ).aggregate(
            Sum('num_words')
        )['num_words__sum']
        # Nothing matches filter
        if self.failing_checks_words is None:
            self.failing_checks_words = 0

        # Store hash will save object
        self.store_hash()

    def store_hash(self):
        '''
        Stores current hash in database.
        '''
        blob_hash = self.get_git_blob_hash()
        self.revision = blob_hash
        self.save()

    def get_last_author(self, email=False):
        '''
        Returns last autor of change done in Weblate.
        '''
        try:
            return self.get_author_name(
                self.change_set.content()[0].author,
                email
            )
        except IndexError:
            return None

    def get_last_change(self):
        '''
        Returns date of last change done in Weblate.
        '''
        try:
            return self.change_set.content()[0].timestamp
        except IndexError:
            return None

    def commit_pending(self, request, author=None, skip_push=False):
        '''
        Commits any pending changes.
        '''
        # Get author of last changes
        last = self.get_last_author(True)

        # If it is same as current one, we don't have to commit
        if author == last or last is None:
            return

        # Commit changes
        self.git_commit(
            request, last, self.get_last_change(), True, True, skip_push
        )

    def get_author_name(self, user, email=True):
        '''
        Returns formatted author name with email.
        '''
        # Get full name from database
        full_name = user.first_name

        # Use username if full name is empty
        if full_name == '':
            full_name = user.username

        # Add email if we are asked for it
        if not email:
            return full_name
        return '%s <%s>' % (full_name, user.email)

    def get_commit_message(self):
        '''
        Formats commit message based on project configuration.
        '''
        msg = self.subproject.project.commit_message % {
            'language': self.language_code,
            'language_name': self.language.name,
            'subproject': self.subproject.name,
            'project': self.subproject.project.name,
            'total': self.total,
            'fuzzy': self.fuzzy,
            'fuzzy_percent': self.get_fuzzy_percent(),
            'translated': self.translated,
            'translated_percent': self.get_translated_percent(),
        }
        if self.commit_message:
            msg = '%s\n\n%s' % (msg, self.commit_message)
            self.commit_message = ''
            self.save()

        return msg

    def __configure_git(self, gitrepo, section, key, expected):
        '''
        Adjusts git config to ensure that section.key is set to expected.
        '''
        cnf = gitrepo.config_writer()
        try:
            # Get value and if it matches we're done
            value = cnf.get(section, key)
            if value == expected:
                return
        except ConfigParser.Error:
            pass

        # Add section if it does not exist
        if not cnf.has_section(section):
            cnf.add_section(section)

        # Update config
        cnf.set(section, key, expected)

    def __configure_committer(self, gitrepo):
        '''
        Wrapper for setting proper committer. As this can not be done by
        passing parameter, we need to check config on every commit.
        '''
        self.__configure_git(
            gitrepo,
            'user',
            'name',
            self.subproject.project.committer_name
        )
        self.__configure_git(
            gitrepo,
            'user',
            'email',
            self.subproject.project.committer_email
        )

    def __git_commit(self, gitrepo, author, timestamp, sync=False):
        '''
        Commits translation to git.
        '''
        # Check git config
        self.__configure_committer(gitrepo)

        # Format commit message
        msg = self.get_commit_message()

        # Pre commit hook
        if self.subproject.pre_commit_script != '':
            ret = os.system('%s "%s"' % (
                self.subproject.pre_commit_script,
                self.get_filename()
            ))
            if ret != 0:
                weblate.logger.error(
                    'Failed to run pre commit script (%d): %s',
                    ret,
                    self.subproject.pre_commit_script
                )

        # Create list of files to commit
        gitrepo.git.add(self.filename)
        if self.subproject.extra_commit_file != '':
            extra_file = self.subproject.extra_commit_file % {
                'language': self.language_code,
            }
            full_path_extra = os.path.join(
                self.subproject.get_path(),
                extra_file
            )
            if os.path.exists(full_path_extra):
                gitrepo.git.add(extra_file)

        # Do actual commit
        gitrepo.git.commit(
            author=author.encode('utf-8'),
            date=timestamp.isoformat(),
            m=msg.encode('utf-8'),
        )

        # Optionally store updated hash
        if sync:
            self.store_hash()

    def git_needs_commit(self):
        '''
        Checks whether there are some not committed changes.
        '''
        status = self.git_repo.git.status('--porcelain', '--', self.filename)
        if status == '':
            # No changes to commit
            return False
        return True

    def git_needs_merge(self):
        return self.subproject.git_needs_merge()

    def git_needs_push(self):
        return self.subproject.git_needs_push()

    def git_commit(self, request, author, timestamp, force_commit=False,
                   sync=False, skip_push=False):
        '''
        Wrapper for commiting translation to git.

        force_commit forces commit with lazy commits enabled

        sync updates git hash stored within the translation (otherwise
        translation rescan will be needed)
        '''
        gitrepo = self.git_repo

        # Is there something for commit?
        if not self.git_needs_commit():
            return False

        # Can we delay commit?
        if not force_commit and appsettings.LAZY_COMMITS:
            weblate.logger.info(
                'Delaying commiting %s in %s as %s',
                self.filename,
                self,
                author
            )
            return False

        # Do actual commit with git lock
        weblate.logger.info(
            'Commiting %s in %s as %s',
            self.filename,
            self,
            author
        )
        with self.subproject.git_lock:
            try:
                self.__git_commit(gitrepo, author, timestamp, sync)
            except git.GitCommandError:
                # There might be another attempt on commit in same time
                # so we will sleep a bit an retry
                sleep_while_git_locked()
                self.__git_commit(gitrepo, author, timestamp, sync)

        # Push if we should
        if (self.subproject.project.push_on_commit
                and not skip_push
                and self.can_push()):
            self.subproject.do_push(request, force_commit=False)

        return True

    def update_unit(self, unit, request, user=None):
        '''
        Updates backend file and unit.
        '''
        if user is None:
            user = request.user
        # Save with lock acquired
        with self.subproject.git_lock:

            src = unit.get_source_plurals()[0]
            add = False

            pounit, add = self.store.find_unit(unit.context, src)

            # Bail out if we have not found anything
            if pounit is None or pounit.is_obsolete():
                return False, None

            # Check for changes
            if (not add
                    and unit.target == pounit.get_target()
                    and unit.fuzzy == pounit.is_fuzzy()):
                return False, pounit

            # Store translations
            if unit.is_plural():
                pounit.set_target(unit.get_target_plurals())
            else:
                pounit.set_target(unit.target)

            # Update fuzzy flag
            pounit.mark_fuzzy(unit.fuzzy)

            # Optionally add unit to translation file
            if add:
                self.store.add_unit(pounit)

            # We need to update backend now
            author = self.get_author_name(user)

            # Update po file header
            po_revision_date = (
                datetime.now().strftime('%Y-%m-%d %H:%M')
                + poheader.tzstring()
            )

            # Prepare headers to update
            headers = {
                'add': True,
                'last_translator': author,
                'plural_forms': self.language.get_plural_form(),
                'language': self.language_code,
                'PO_Revision_Date': po_revision_date,
            }

            # Optionally store language team with link to website
            if self.subproject.project.set_translation_team:
                headers['language_team'] = '%s <%s>' % (
                    self.language.name,
                    get_site_url(self.get_absolute_url()),
                )

            # Optionally store email for reporting bugs in source
            report_source_bugs = self.subproject.report_source_bugs
            if report_source_bugs != '':
                headers['report_msgid_bugs_to'] = report_source_bugs

            # Update genric headers
            self.store.update_header(
                **headers
            )

            # commit possible previous changes (by other author)
            self.commit_pending(request, author)
            # save translation changes
            self.store.save()
            # commit Git repo if needed
            self.git_commit(request, author, timezone.now(), sync=True)

        return True, pounit

    def get_source_checks(self):
        '''
        Returns list of failing source checks on current subproject.
        '''
        result = [('all', _('All strings'))]

        # All checks
        sourcechecks = self.unit_set.count_type('sourcechecks', self)
        if sourcechecks > 0:
            result.append((
                'sourcechecks',
                _('Strings with any failing checks (%d)') % sourcechecks
            ))

        # Process specific checks
        for check in CHECKS:
            if not CHECKS[check].source:
                continue
            cnt = self.get_failing_checks(check)
            if cnt > 0:
                desc = CHECKS[check].description + (' (%d)' % cnt)
                result.append((check, desc))

        # Grab comments
        sourcecomments = self.unit_set.count_type('sourcecomments', self)
        if sourcecomments > 0:
            result.append((
                'sourcecomments',
                _('Strings with comments (%d)') % sourcecomments
            ))

        return result

    def get_translation_checks(self):
        '''
        Returns list of failing checks on current translation.
        '''
        result = [('all', _('All strings'))]

        # Untranslated strings
        nottranslated = self.unit_set.count_type('untranslated', self)
        if nottranslated > 0:
            result.append((
                'untranslated',
                _('Untranslated strings (%d)') % nottranslated
            ))

        # Fuzzy strings
        fuzzy = self.unit_set.count_type('fuzzy', self)
        if fuzzy > 0:
            result.append((
                'fuzzy',
                _('Fuzzy strings (%d)') % fuzzy
            ))

        # Translations with suggestions
        if self.have_suggestion > 0:
            result.append((
                'suggestions',
                _('Strings with suggestions (%d)') % self.have_suggestion
            ))

        # All checks
        if self.failing_checks > 0:
            result.append((
                'allchecks',
                _('Strings with any failing checks (%d)') % self.failing_checks
            ))

        # Process specific checks
        for check in CHECKS:
            if not CHECKS[check].target:
                continue
            cnt = self.unit_set.count_type(check, self)
            if cnt > 0:
                desc = CHECKS[check].description + (' (%d)' % cnt)
                result.append((check, desc))

        # Grab comments
        targetcomments = self.unit_set.count_type('targetcomments', self)
        if targetcomments > 0:
            result.append((
                'targetcomments',
                _('Strings with comments (%d)') % targetcomments
            ))

        return result

    def merge_translations(self, request, author, store2, overwrite,
                           add_fuzzy):
        """
        Merges translation unit wise, needed for template based translations to
        add new strings.
        """

        for unit2 in store2.all_units():
            # No translated -> skip
            if not unit2.is_translated() or unit2.unit.isheader():
                continue

            try:
                unit = self.unit_set.get(
                    source=unit2.get_source(),
                    context=unit2.get_context(),
                )
            except Unit.DoesNotExist:
                continue

            unit.translate(
                request,
                split_plural(unit2.get_target()),
                add_fuzzy or unit2.is_fuzzy()
            )

    def merge_store(self, request, author, store2, overwrite, merge_header,
                    add_fuzzy):
        '''
        Merges translate-toolkit store into current translation.
        '''
        # Merge with lock acquired
        with self.subproject.git_lock:

            store1 = self.store.store
            store1.require_index()

            for unit2 in store2.all_units():
                # No translated -> skip
                if not unit2.is_translated():
                    continue

                # Optionally merge header
                if unit2.unit.isheader():
                    if merge_header and isinstance(store1, poheader.poheader):
                        store1.mergeheaders(store2)
                    continue

                # Find unit by ID
                unit1 = store1.findid(unit2.unit.getid())

                # Fallback to finding by source
                if unit1 is None:
                    unit1 = store1.findunit(unit2.unit.source)

                # Unit not found, nothing to do
                if unit1 is None:
                    continue

                # Should we overwrite
                if not overwrite and unit1.istranslated():
                    continue

                # Actually update translation
                unit1.merge(unit2.unit, overwrite=True, comments=False)

                # Handle
                if add_fuzzy:
                    unit1.markfuzzy()

            # Write to backend and commit
            self.commit_pending(request, author)
            store1.save()
            ret = self.git_commit(request, author, timezone.now(), True)
            self.check_sync(request=request, change=Change.ACTION_UPLOAD)

        return ret

    def merge_suggestions(self, request, store):
        '''
        Merges content of translate-toolkit store as a suggestions.
        '''
        ret = False
        for unit in store.all_units():

            # Skip headers or not translated
            if not unit.is_translatable() or not unit.is_translated():
                continue

            # Calculate unit checksum
            checksum = unit.get_checksum()

            # Grab database unit
            try:
                dbunit = self.unit_set.filter(checksum=checksum)[0]
            except Unit.DoesNotExist:
                continue

            # Indicate something new
            ret = True

            # Add suggestion
            Suggestion.objects.add(dbunit, unit.get_target(), request)

        # Update suggestion count
        if ret:
            self.update_stats()

        return ret

    def merge_upload(self, request, fileobj, overwrite, author=None,
                     merge_header=True, method=''):
        '''
        Top level handler for file uploads.
        '''
        # Load backend file
        try:
            # First try using own loader
            store = self.subproject.file_format_cls(
                fileobj,
                self.subproject.template_store
            )
        except Exception:
            # Fallback to automatic detection
            fileobj.seek(0)
            store = AutoFormat(fileobj)

        # Optionally set authorship
        if author is None:
            author = self.get_author_name(request.user)

        # List translations we should process
        # Filter out those who don't want automatic update, but keep ourselves
        translations = Translation.objects.filter(
            language=self.language,
            subproject__project=self.subproject.project
        ).filter(
            Q(pk=self.pk) | Q(subproject__allow_translation_propagation=True)
        )

        ret = False

        if method in ('', 'fuzzy'):
            # Do actual merge
            if self.subproject.has_template():
                # Merge on units level
                self.merge_translations(
                    request,
                    author,
                    store,
                    overwrite,
                    (method == 'fuzzy')
                )
            else:
                # Merge on file level
                for translation in translations:
                    ret |= translation.merge_store(
                        request,
                        author,
                        store,
                        overwrite,
                        merge_header,
                        (method == 'fuzzy')
                    )
        else:
            # Add as sugestions
            ret = self.merge_suggestions(request, store)

        return ret, store.count_units()

    def get_failing_checks(self, check):
        '''
        Returns number of units with failing checks.

        By default for all checks or check type can be specified.
        '''
        return self.unit_set.count_type(check, self)

    def invalidate_cache(self, cache_type=None):
        '''
        Invalidates any cached stats.
        '''
        # Get parts of key cache
        slug = self.subproject.get_full_slug()
        code = self.language.code

        # Are we asked for specific cache key?
        if cache_type is None:
            keys = list(CHECKS)
        else:
            keys = [cache_type]

        # Actually delete the cache
        for rqtype in keys:
            cache_key = 'counts-%s-%s-%s' % (slug, code, rqtype)
            cache.delete(cache_key)

    def get_kwargs(self):
        return {
            'lang': self.language.code,
            'subproject': self.subproject.slug,
            'project': self.subproject.project.slug
        }
