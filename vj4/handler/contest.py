import os
import asyncio
import calendar
import collections
import datetime
import functools
import io
import logging
import pytz
import yaml
import zipfile
from bson import objectid

from vj4 import app
from vj4 import constant
from vj4 import error
from vj4.model import builtin
from vj4.model import document
from vj4.model import opcount
from vj4.model import record
from vj4.model import user
from vj4.model.adaptor import discussion
from vj4.model.adaptor import contest
from vj4.model.adaptor import moss
from vj4.model.adaptor import problem
from vj4.handler import base
from vj4.util import pagination, options
from vj4.util.misc import filter_language, filter_content_type

_logger = logging.getLogger(__name__)


def _parse_pids(pids_str):
    pid_list = list(map(document.convert_doc_id, pids_str.split(',')))
    pid_set = set()
    pids = []
    for pid in pid_list:
        if not pid in pid_set:
            pid_set.add(pid)
            pids.append(pid)
    return pids


def _format_pids(pids_list):
    return ','.join([str(pid) for pid in pids_list])


def _parse_penalty_rules_yaml(penalty_rules):
    try:
        penalty_rules = yaml.safe_load(penalty_rules)
    except yaml.YAMLError:
        raise error.ValidationError('penalty_rules', 'parse error')
    if not isinstance(penalty_rules, dict):
        raise error.ValidationError('penalty_rules', 'invalid format')
    new_rules = collections.OrderedDict()
    try:
        for time, coefficient in sorted(penalty_rules.items(), key=lambda x: float(x[0])):
            time_val = str(int(float(time) * 60 * 60))
            coefficient_val = float(coefficient)
            new_rules[time_val] = coefficient_val
    except ValueError:
        raise error.ValidationError('penalty_rules', 'value error')
    return new_rules


def _format_penalty_rules_yaml(penalty_rules):
    # yaml does not support dump from ordereddict so that we output yaml document manually here
    yaml_doc = ''
    for time, coefficient in sorted(penalty_rules.items(), key=lambda x: int(x[0])):
        time_val = round(float(time) / 60 / 60, ndigits=2)
        yaml_doc += '{0}: {1}\n'.format(time_val, coefficient)
    return yaml_doc


class ContestPageCategoryMixin(object):
    @property
    @base.route_argument
    def page_category(self, ctype: str, **kwargs):
        return ctype


class ContestStatusMixin(object):
    @property
    @functools.lru_cache()
    def now(self):
        # TODO(iceboy): This does not work on multi-machine environment.
        return datetime.datetime.utcnow()

    def is_not_started(self, tdoc):
        return self.now < tdoc['begin_at']

    def is_ongoing(self, tdoc):
        return tdoc['begin_at'] <= self.now < tdoc['end_at']

    def is_finished(self, tdoc):
        return self.now >= tdoc['end_at']

    def is_upcoming(self, tdoc):
        ref = tdoc['begin_at'] - datetime.timedelta(days=1)
        return ref <= self.now < tdoc['begin_at']

    def is_homework_extended(self, tdoc):
        return tdoc['penalty_since'] <= self.now < tdoc['end_at']

    def get_status(self, tdoc):
        if self.is_not_started(tdoc):
            return 'not_started'
        elif self.is_ongoing(tdoc):
            return 'ongoing'
        else:
            return 'finished'


class ContestVisibilityMixin(object):
    def can_view_hidden_scoreboard(self, tdoc):
        if tdoc['doc_type'] == document.TYPE_CONTEST:
            return self.has_perm(builtin.PERM_VIEW_CONTEST_HIDDEN_SCOREBOARD)
        elif tdoc['doc_type'] == document.TYPE_HOMEWORK:
            return self.has_perm(builtin.PERM_VIEW_HOMEWORK_HIDDEN_SCOREBOARD)
        else:
            return False

    def can_show_record(self, tdoc, allow_perm_override=True):
        if contest.RULES[tdoc['rule']].show_record_func(tdoc, datetime.datetime.utcnow()):
            return True
        if allow_perm_override and self.can_view_hidden_scoreboard(tdoc):
            return True
        return False

    def can_show_scoreboard(self, tdoc, allow_perm_override=True):
        if contest.RULES[tdoc['rule']].show_scoreboard_func(tdoc, datetime.datetime.utcnow()):
            return True
        if allow_perm_override and self.can_view_hidden_scoreboard(tdoc):
            return True
        return False


class ContestCommonOperationMixin(object):
    async def get_scoreboard(self, doc_type: int, tid: objectid.ObjectId, is_export: bool = False):
        tdoc, tsdocs = await contest.get_and_list_status(self.domain_id, doc_type, tid, is_export=is_export)
        if not self.can_show_scoreboard(tdoc):
            if doc_type == document.TYPE_CONTEST:
                raise error.ContestScoreboardHiddenError(self.domain_id, tid)
            elif doc_type == document.TYPE_HOMEWORK:
                raise error.HomeworkScoreboardHiddenError(self.domain_id, tid)
            else:
                raise error.InvalidArgumentError('doc_type')
        udict, pdict = await asyncio.gather(user.get_dict([tsdoc['uid'] for tsdoc in tsdocs]),
                                            problem.get_dict(self.domain_id, tdoc['pids']))
        ranked_tsdocs = contest.RULES[tdoc['rule']].rank_func(tsdocs)
        rows = contest.RULES[tdoc['rule']].scoreboard_func(is_export, self.translate, tdoc,
                                                           ranked_tsdocs, udict, pdict)
        return tdoc, rows

    async def verify_problems(self, pids):
        pdocs = await problem.get_multi(domain_id=self.domain_id, doc_id={'$in': pids},
                                        fields={'doc_id': 1}) \
            .sort('doc_id', 1) \
            .to_list()
        exist_pids = [pdoc['doc_id'] for pdoc in pdocs]
        if len(pids) != len(exist_pids):
            for pid in pids:
                if pid not in exist_pids:
                    raise error.ProblemNotFoundError(self.domain_id, pid)
        return pids

    async def hide_problems(self, pids):
        for pid in pids:
            await problem.set_hidden(self.domain_id, pid, True)

    async def get_latest_records(self, doc_type, tid, break_func=None, skip_func=None):
        tdoc, tsdocs = await contest.get_and_list_status(self.domain_id, doc_type, tid)
        result = []

        for tsdoc in tsdocs:
            # continue if no record found
            if not tsdoc.get('journal'):
                continue

            rids = list(map(lambda x: x['rid'], tsdoc['journal']))
            rdocs = record.get_multi(get_hidden=True, _id={'$in': rids}).sort([('_id', -1)])

            pids = set(tdoc.get('pids'))
            async for rdoc in rdocs:
                if break_func and break_func(rdoc):
                    break
                if skip_func and skip_func(rdoc):
                    continue

                # when all problem have a record, break
                if len(pids) == 0:
                    break
                # we only need to get one record for each problem
                if rdoc['pid'] not in pids:
                    continue

                pids.remove(rdoc['pid'])
                result.append(rdoc)

        return result


class ContestMixin(ContestStatusMixin, ContestVisibilityMixin, ContestCommonOperationMixin):
    pass


@app.route('/{ctype:contest|homework}', 'contest_main')
class ContestMainHandler(ContestMixin, ContestPageCategoryMixin, base.Handler):
    CONTESTS_PER_PAGE = 20

    @base.route_argument
    async def get(self, *, ctype: str):
        if ctype == 'homework':
            await self._get_homework()
        elif ctype == 'contest':
            await self._get_contest()
        else:
            raise error.InvalidArgumentError('ctype')

    @base.require_perm(builtin.PERM_VIEW_CONTEST)
    @base.get_argument
    @base.sanitize
    async def _get_contest(self, *, rule: int = 0, page: int = 1):
        if not rule:
            tdocs = contest.get_multi(self.domain_id, document.TYPE_CONTEST)
            qs = ''
        else:
            if rule not in constant.contest.CONTEST_RULES:
                raise error.ValidationError('rule')
            tdocs = contest.get_multi(self.domain_id, document.TYPE_CONTEST, rule=rule)
            qs = 'rule={0}'.format(rule)
        tdocs, tpcount, _ = await pagination.paginate(tdocs, page, self.CONTESTS_PER_PAGE)
        tsdict = await contest.get_dict_status(self.domain_id, self.user['_id'],
                                               (tdoc['doc_id'] for tdoc in tdocs))
        page_title = self.translate('page.contest_main.contest.title')
        path_components = self.build_path((page_title, None))
        self.render('contest_main.html', page=page, tpcount=tpcount, qs=qs, rule=rule,
                    tdocs=tdocs, tsdict=tsdict,
                    page_title=page_title, path_components=path_components)

    @base.require_perm(builtin.PERM_VIEW_HOMEWORK)
    async def _get_homework(self):
        tdocs = await contest.get_multi(self.domain_id, document.TYPE_HOMEWORK).to_list()
        calendar_tdocs = []
        for tdoc in tdocs:
            cal_tdoc = {'id': tdoc['doc_id'],
                        'begin_at': self.datetime_stamp(tdoc['begin_at']),
                        'title': tdoc['title'],
                        'status': self.get_status(tdoc),
                        'url': self.reverse_url('contest_detail', ctype='homework', tid=tdoc['doc_id'])}
            if self.is_homework_extended(tdoc) or self.is_finished(tdoc):
                cal_tdoc['end_at'] = self.datetime_stamp(tdoc['end_at'])
                cal_tdoc['penalty_since'] = self.datetime_stamp(tdoc['penalty_since'])
            else:
                cal_tdoc['end_at'] = self.datetime_stamp(tdoc['penalty_since'])
            calendar_tdocs.append(cal_tdoc)
        page_title = self.translate('page.contest_main.homework.title')
        path_components = self.build_path((page_title, None))
        self.render('homework_main.html', tdocs=tdocs, calendar_tdocs=calendar_tdocs,
                    page_title=page_title, path_components=path_components)


@app.route('/{ctype:contest|homework}/{tid:\w{24}}', 'contest_detail')
class ContestDetailHandler(ContestMixin, ContestPageCategoryMixin, base.OperationHandler):
    DISCUSSIONS_PER_PAGE = 15

    @base.require_perm(builtin.PERM_VIEW_CONTEST)
    @base.get_argument
    @base.route_argument
    @base.sanitize
    @base.require_perm(builtin.PERM_VIEW_CONTEST, when=lambda ctype, **kwargs: ctype == 'contest')
    @base.require_perm(builtin.PERM_VIEW_HOMEWORK, when=lambda ctype, **kwargs: ctype == 'homework')
    async def get(self, *, ctype: str, tid: objectid.ObjectId, page: int = 1):
        doc_type = constant.contest.CTYPE_TO_DOCTYPE[ctype]
        tdoc = await contest.get(self.domain_id, doc_type, tid)
        tsdoc, pdict = await asyncio.gather(
            contest.get_status(self.domain_id, doc_type, tdoc['doc_id'], self.user['_id']),
            problem.get_dict(self.domain_id, tdoc['pids']))
        psdict = dict()
        rdict = dict()
        if tsdoc:
            attended = tsdoc.get('attend') == 1
            for pdetail in tsdoc.get('detail', []):
                psdict[pdetail['pid']] = pdetail
            if self.can_show_record(tdoc):
                rdict = await record.get_dict((psdoc['rid'] for psdoc in psdict.values()),
                                              get_hidden=True)
            else:
                rdict = dict((psdoc['rid'], {'_id': psdoc['rid']}) for psdoc in psdict.values())
        else:
            attended = False
        # discussion
        ddocs, dpcount, dcount = await pagination.paginate(
            discussion.get_multi(self.domain_id,
                                 parent_doc_type=tdoc['doc_type'],
                                 parent_doc_id=tdoc['doc_id']),
            page, self.DISCUSSIONS_PER_PAGE)
        uids = set(ddoc['owner_uid'] for ddoc in ddocs)
        uids.add(tdoc['owner_uid'])
        udict = await user.get_dict(uids)
        path_components = self.build_path(
            (
                self.translate('page.contest_main.{0}.title'.format(ctype)),
                self.reverse_url('contest_main', ctype=ctype)),
            (tdoc['title'], None))
        self.render('{0}_detail.html'.format(ctype), tdoc=tdoc, tsdoc=tsdoc, attended=attended, udict=udict,
                    pdict=pdict, psdict=psdict, rdict=rdict,
                    ddocs=ddocs, page=page, dpcount=dpcount, dcount=dcount,
                    datetime_stamp=self.datetime_stamp,
                    page_title=tdoc['title'], path_components=path_components)

    @base.require_priv(builtin.PRIV_USER_PROFILE)
    @base.require_perm(builtin.PERM_ATTEND_CONTEST)
    @base.route_argument
    @base.require_csrf_token
    @base.sanitize
    @base.require_priv(builtin.PRIV_USER_PROFILE)
    @base.require_perm(builtin.PERM_ATTEND_CONTEST, when=lambda ctype, **kwargs: ctype == 'contest')
    @base.require_perm(builtin.PERM_ATTEND_HOMEWORK, when=lambda ctype, **kwargs: ctype == 'homework')
    async def post_attend(self, *, ctype: str, tid: objectid.ObjectId):
        doc_type = constant.contest.CTYPE_TO_DOCTYPE[ctype]
        tdoc = await contest.get(self.domain_id, doc_type, tid)
        if self.is_finished(tdoc):
            raise error.ContestNotLiveError(tdoc['doc_id'])
        await contest.attend(self.domain_id, doc_type, tdoc['doc_id'], self.user['_id'])
        self.json_or_redirect(self.url)


@app.route('/{ctype:contest|homework}/{tid:\w{24}}/code', 'contest_code')
class ContestCodeHandler(base.OperationHandler):
    @base.route_argument
    @base.sanitize
    @base.limit_rate('contest_code', 3600, 60)
    @base.require_perm(builtin.PERM_VIEW_CONTEST, when=lambda ctype, **kwargs: ctype == 'contest')
    @base.require_perm(builtin.PERM_VIEW_HOMEWORK, when=lambda ctype, **kwargs: ctype == 'homework')
    @base.require_perm(builtin.PERM_READ_RECORD_CODE)
    async def get(self, *, ctype: str, tid: objectid.ObjectId):
        tdoc, tsdocs = await contest.get_and_list_status(self.domain_id,
                                                         {'$in': [document.TYPE_CONTEST, document.TYPE_HOMEWORK]}, tid)
        rnames = {}
        for tsdoc in tsdocs:
            for pdetail in tsdoc.get('detail', []):
                rnames[pdetail['rid']] = 'U{}_P{}_R{}'.format(tsdoc['uid'], pdetail['pid'], pdetail['rid'])
        output_buffer = io.BytesIO()
        zip_file = zipfile.ZipFile(output_buffer, 'a', zipfile.ZIP_DEFLATED)
        rdocs = record.get_multi(get_hidden=True, _id={'$in': list(rnames.keys())})
        async for rdoc in rdocs:
            zip_file.writestr(rnames[rdoc['_id']] + '.' + rdoc['lang'], rdoc['code'])
        # mark all files as created in Windows :p
        for zfile in zip_file.filelist:
            zfile.create_system = 0
        zip_file.close()

        await self.binary(output_buffer.getvalue(), 'application/zip',
                          file_name='{}.zip'.format(tdoc['title']))


@app.route('/{ctype:contest|homework}/{tid}/{pid:-?\d+|\w{24}}', 'contest_detail_problem')
class ContestDetailProblemHandler(ContestMixin, ContestPageCategoryMixin, base.Handler):
    @base.route_argument
    @base.sanitize
    @base.require_perm(builtin.PERM_VIEW_CONTEST, when=lambda ctype, **kwargs: ctype == 'contest')
    @base.require_perm(builtin.PERM_VIEW_HOMEWORK, when=lambda ctype, **kwargs: ctype == 'homework')
    @base.require_perm(builtin.PERM_VIEW_PROBLEM)
    async def get(self, *, ctype: str, tid: objectid.ObjectId, pid: document.convert_doc_id):
        doc_type = constant.contest.CTYPE_TO_DOCTYPE[ctype]
        uid = self.user['_id'] if self.has_priv(builtin.PRIV_USER_PROFILE) else None
        tdoc, pdoc = await asyncio.gather(contest.get(self.domain_id, doc_type, tid),
                                          problem.get(self.domain_id, pid, uid))
        tsdoc, udoc = await asyncio.gather(
            contest.get_status(self.domain_id, doc_type, tdoc['doc_id'], self.user['_id']),
            user.get_by_uid(tdoc['owner_uid']))
        attended = tsdoc and tsdoc.get('attend') == 1
        if not self.is_finished(tdoc):
            if not attended:
                if ctype == 'contest':
                    raise error.ContestNotAttendedError(tdoc['doc_id'])
                elif ctype == 'homework':
                    raise error.HomeworkNotAttendedError(tdoc['doc_id'])
                else:
                    raise error.InvalidArgumentError('ctype')
            if not self.is_ongoing(tdoc):
                if ctype == 'contest':
                    raise error.ContestNotLiveError(tdoc['doc_id'])
                elif ctype == 'homework':
                    raise error.HomeworkNotLiveError(tdoc['doc_id'])
                else:
                    raise error.InvalidArgumentError('ctype')
        if pid not in tdoc['pids']:
            raise error.ProblemNotFoundError(self.domain_id, pid, tdoc['doc_id'])
        path_components = self.build_path(
            (
                self.translate('page.contest_main.{0}.title'.format(ctype)),
                self.reverse_url('contest_main', ctype=ctype)),
            (tdoc['title'], self.reverse_url('contest_detail', ctype=ctype, tid=tid)),
            (pdoc['title'], None))
        self.render('problem_detail.html', tdoc=tdoc, pdoc=pdoc, tsdoc=tsdoc, udoc=udoc,
                    attended=attended,
                    page_title=pdoc['title'], path_components=path_components)


@app.route('/{ctype:contest|homework}/{tid}/{pid}/submit', 'contest_detail_problem_submit')
class ContestDetailProblemSubmitHandler(ContestMixin, ContestPageCategoryMixin, base.Handler):
    def get_content_type(self, filename):
        type, file_type = filter_content_type(filename, [
            constant.record.FILE_TYPE_TAR,
            constant.record.FILE_TYPE_ZIP,
            constant.record.FILE_TYPE_RAR
        ])
        self.file_type = file_type
        return type

    @base.route_argument
    @base.sanitize
    @base.require_perm(builtin.PERM_VIEW_CONTEST, when=lambda ctype, **kwargs: ctype == 'contest')
    @base.require_perm(builtin.PERM_VIEW_HOMEWORK, when=lambda ctype, **kwargs: ctype == 'homework')
    @base.require_perm(builtin.PERM_SUBMIT_PROBLEM)
    async def get(self, *, ctype: str, tid: objectid.ObjectId, pid: document.convert_doc_id):
        doc_type = constant.contest.CTYPE_TO_DOCTYPE[ctype]
        uid = self.user['_id'] if self.has_priv(builtin.PRIV_USER_PROFILE) else None
        tdoc, pdoc = await asyncio.gather(contest.get(self.domain_id, doc_type, tid),
                                          problem.get(self.domain_id, pid, uid))
        tsdoc, udoc = await asyncio.gather(
            contest.get_status(self.domain_id, doc_type, tdoc['doc_id'], self.user['_id']),
            user.get_by_uid(tdoc['owner_uid']))
        attended = tsdoc and tsdoc.get('attend') == 1
        if not attended:
            if ctype == 'contest':
                raise error.ContestNotAttendedError(tdoc['doc_id'])
            elif ctype == 'homework':
                raise error.HomeworkNotAttendedError(tdoc['doc_id'])
            else:
                raise error.InvalidArgumentError('ctype')
        if not self.is_ongoing(tdoc):
            if ctype == 'contest':
                raise error.ContestNotLiveError(tdoc['doc_id'])
            elif ctype == 'homework':
                raise error.HomeworkNotLiveError(tdoc['doc_id'])
            else:
                raise error.InvalidArgumentError('ctype')
        if pid not in tdoc['pids']:
            raise error.ProblemNotFoundError(self.domain_id, pid, tdoc['doc_id'])
        if self.can_show_record(tdoc):
            rdocs = await record.get_user_in_problem_multi(uid, self.domain_id, pdoc['doc_id'], get_hidden=True) \
                .sort([('_id', -1)]) \
                .limit(10) \
                .to_list()
        else:
            rdocs = []
        if not self.prefer_json:
            path_components = self.build_path(
                (self.translate('page.contest_main.{0}.title'.format(ctype)),
                 self.reverse_url('contest_main', ctype=ctype)),
                (tdoc['title'], self.reverse_url('contest_detail', ctype=ctype, tid=tid)),
                (pdoc['title'], self.reverse_url('contest_detail_problem', ctype=ctype, tid=tid, pid=pid)),
                (self.translate('page.contest_detail_problem_submit.{0}.title'.format(ctype)), None))
            languages = filter_language(pdoc.get('languages') or [])
            default_lang = len(languages) and list(languages.keys())[0] or ''
            self.render('problem_submit.html', tdoc=tdoc, pdoc=pdoc, rdocs=rdocs,
                        tsdoc=tsdoc, udoc=udoc, attended=attended,
                        page_title=pdoc['title'], path_components=path_components,
                        languages=languages, default_lang=default_lang)
        else:
            self.json({'rdocs': rdocs})

    @base.require_priv(builtin.PRIV_USER_PROFILE)
    @base.require_perm(builtin.PERM_VIEW_CONTEST)
    @base.require_perm(builtin.PERM_SUBMIT_PROBLEM)
    @base.route_argument
    @base.multipart_argument
    @base.require_csrf_token
    @base.sanitize
    @base.require_priv(builtin.PRIV_USER_PROFILE)
    @base.require_perm(builtin.PERM_VIEW_CONTEST, when=lambda ctype, **kwargs: ctype == 'contest')
    @base.require_perm(builtin.PERM_VIEW_HOMEWORK, when=lambda ctype, **kwargs: ctype == 'homework')
    @base.require_perm(builtin.PERM_SUBMIT_PROBLEM)
    @base.limit_rate('add_record', 60, 100)
    @base.limit_rate_homework('add_record', 24)
    @base.limit_file_size('code', 2)
    async def post(self, *, ctype: str, tid: objectid.ObjectId, pid: document.convert_doc_id,
                   lang: str, code: objectid.ObjectId):
        # TODO(tc-imba) add the file size limit as a setting

        doc_type = constant.contest.CTYPE_TO_DOCTYPE[ctype]
        # TODO(iceboy): rate limit base on ip.
        tdoc, pdoc = await asyncio.gather(contest.get(self.domain_id, doc_type, tid),
                                          problem.get(self.domain_id, pid))
        tsdoc = await contest.get_status(self.domain_id, doc_type, tdoc['doc_id'], self.user['_id'])
        if not tsdoc or tsdoc.get('attend') != 1:
            if ctype == 'contest':
                raise error.ContestNotAttendedError(tdoc['doc_id'])
            elif ctype == 'homework':
                raise error.HomeworkNotAttendedError(tdoc['doc_id'])
            else:
                raise error.InvalidArgumentError('ctype')
        if not self.is_ongoing(tdoc):
            if ctype == 'contest':
                raise error.ContestNotLiveError(tdoc['doc_id'])
            elif ctype == 'homework':
                raise error.HomeworkNotLiveError(tdoc['doc_id'])
            else:
                raise error.InvalidArgumentError('ctype')
        if pid not in tdoc['pids']:
            raise error.ProblemNotFoundError(self.domain_id, pid, tdoc['doc_id'])

        # TODO(tc-imba): only constant.record.FILE_TYPE_TAR and constant.record.FILE_TYPE_ZIP is supported now
        code_type = self.file_type or constant.record.FILE_TYPE_TEXT
        show_detail = 'show_case_detail' in pdoc and pdoc['show_case_detail']
        if lang not in (pdoc.get('languages') or []):
            raise error.InvalidArgumentError('lang')
        rid = await record.add(self.domain_id, pdoc['doc_id'], constant.record.TYPE_SUBMISSION,
                               self.user['_id'], lang, code, tid=tdoc['doc_id'], hidden=False,
                               code_type=code_type, show_detail=show_detail)

        await contest.update_status(self.domain_id, tdoc['doc_id'], self.user['_id'],
                                    rid, pdoc['doc_id'], False, 0)
        if not self.can_show_record(tdoc):
            self.json_or_redirect(self.reverse_url('contest_detail', ctype=ctype, tid=tdoc['doc_id']))
        else:
            self.json_or_redirect(self.reverse_url('record_detail', rid=rid))


@app.route('/{ctype:contest|homework}/{tid}/scoreboard', 'contest_scoreboard')
class ContestScoreboardHandler(ContestMixin, ContestPageCategoryMixin, base.Handler):
    @base.route_argument
    @base.sanitize
    @base.require_perm(builtin.PERM_VIEW_CONTEST, when=lambda ctype, **kwargs: ctype == 'contest')
    @base.require_perm(builtin.PERM_VIEW_CONTEST_SCOREBOARD, when=lambda ctype, **kwargs: ctype == 'contest')
    @base.require_perm(builtin.PERM_VIEW_HOMEWORK, when=lambda ctype, **kwargs: ctype == 'homework')
    @base.require_perm(builtin.PERM_VIEW_HOMEWORK_SCOREBOARD, when=lambda ctype, **kwargs: ctype == 'homework')
    async def get(self, *, ctype: str, tid: objectid.ObjectId):
        tdoc, rows = await self.get_scoreboard(constant.contest.CTYPE_TO_DOCTYPE[ctype], tid)
        page_title = self.translate('page.contest_scoreboard.{0}.title'.format(ctype))
        path_components = self.build_path(
            (
                self.translate('page.contest_main.{0}.title'.format(ctype)),
                self.reverse_url('contest_main', ctype=ctype)),
            (tdoc['title'], self.reverse_url('contest_detail', ctype=ctype, tid=tdoc['doc_id'])),
            (page_title, None))
        self.render('contest_scoreboard.html', tdoc=tdoc, rows=rows,
                    page_title=page_title, path_components=path_components)


@app.route('/{ctype:contest|homework}/{tid}/scoreboard/download/{ext}', 'contest_scoreboard_download')
class ContestScoreboardDownloadHandler(ContestMixin, base.Handler):
    def _export_status_as_csv(self, rows):
        # \r\n for notepad compatibility
        csv_content = '\r\n'.join([','.join([str(c['value']) for c in row]) for row in rows])
        data = '\uFEFF' + csv_content
        return data.encode()

    def _export_status_as_html(self, rows):
        return self.render_html('contest_scoreboard_download_html.html', rows=rows).encode()

    @base.require_perm(builtin.PERM_VIEW_CONTEST)
    @base.require_perm(builtin.PERM_VIEW_CONTEST_SCOREBOARD)
    @base.route_argument
    @base.sanitize
    @base.require_perm(builtin.PERM_VIEW_CONTEST, when=lambda ctype, **kwargs: ctype == 'contest')
    @base.require_perm(builtin.PERM_VIEW_CONTEST_SCOREBOARD, when=lambda ctype, **kwargs: ctype == 'contest')
    @base.require_perm(builtin.PERM_VIEW_HOMEWORK, when=lambda ctype, **kwargs: ctype == 'homework')
    @base.require_perm(builtin.PERM_VIEW_HOMEWORK_SCOREBOARD, when=lambda ctype, **kwargs: ctype == 'homework')
    async def get(self, *, ctype: str, tid: objectid.ObjectId, ext: str):
        get_status_content = {
            'csv': self._export_status_as_csv,
            'html': self._export_status_as_html,
        }
        if ext not in get_status_content:
            raise error.ValidationError('ext')
        tdoc, rows = await self.get_scoreboard(constant.contest.CTYPE_TO_DOCTYPE[ctype], tid, True)
        data = get_status_content[ext](rows)
        file_name = tdoc['title']
        await self.binary(data, file_name='{}.{}'.format(file_name, ext))


@app.route('/{ctype:contest|homework}/create', 'contest_create')
class ContestCreateHandler(ContestMixin, ContestPageCategoryMixin, base.Handler):
    @base.route_argument
    async def get(self, *, ctype: str):
        if ctype == 'homework':
            await self._get_homework()
        elif ctype == 'contest':
            await self._get_contest()
        else:
            raise error.InvalidArgumentError('ctype')

    @base.require_priv(builtin.PRIV_USER_PROFILE)
    @base.require_perm(builtin.PERM_CREATE_CONTEST)
    async def _get_contest(self):
        dt = self.now.replace(tzinfo=pytz.utc).astimezone(self.timezone)
        ts = calendar.timegm(dt.utctimetuple())
        # find next quarter
        ts = ts - ts % (15 * 60) + 15 * 60
        dt = datetime.datetime.fromtimestamp(ts, self.timezone)
        page_title = self.translate('page.contest_create.contest.title')
        path_components = self.build_path((page_title, None))
        self.render('contest_edit.html',
                    date_text=dt.strftime('%Y-%m-%d'),
                    time_text=dt.strftime('%H:%M'),
                    pids=_format_pids([1000, 1001]),
                    page_title=page_title, path_components=path_components)

    @base.require_priv(builtin.PRIV_USER_PROFILE)
    @base.require_perm(builtin.PERM_CREATE_HOMEWORK)
    async def _get_homework(self):
        begin_at = self.now.replace(tzinfo=pytz.utc).astimezone(self.timezone) + datetime.timedelta(days=1)
        penalty_since = begin_at + datetime.timedelta(days=7)
        page_title = self.translate('page.contest_create.homework.title')
        path_components = self.build_path((page_title, None))
        self.render('homework_edit.html',
                    date_begin_text=begin_at.strftime('%Y-%m-%d'),
                    time_begin_text='00:00',
                    date_penalty_text=penalty_since.strftime('%Y-%m-%d'),
                    time_penalty_text='23:59',
                    pids=_format_pids([1000, 1001]),
                    extension_days='1',
                    page_title=page_title, path_components=path_components)

    @base.route_argument
    async def post(self, *, ctype: str, **kwargs):
        if ctype == 'homework':
            await self._post_homework()
        elif ctype == 'contest':
            await self._post_contest()
        else:
            raise error.InvalidArgumentError('ctype')

    @base.require_priv(builtin.PRIV_USER_PROFILE)
    @base.require_perm(builtin.PERM_EDIT_PROBLEM)
    @base.require_perm(builtin.PERM_CREATE_CONTEST)
    @base.route_argument
    @base.post_argument
    @base.require_csrf_token
    @base.sanitize
    async def _post_contest(self, *, ctype: str, title: str, content: str, rule: int,
                            begin_at_date: str, begin_at_time: str, duration: float,
                            pids: str):
        try:
            begin_at = datetime.datetime.strptime(begin_at_date + ' ' + begin_at_time, '%Y-%m-%d %H:%M')
            begin_at = self.timezone.localize(begin_at).astimezone(pytz.utc).replace(tzinfo=None)
        except ValueError:
            raise error.ValidationError('begin_at_date', 'begin_at_time')
        end_at = begin_at + datetime.timedelta(hours=duration)
        if begin_at >= end_at:
            raise error.ValidationError('duration')
        pids = _parse_pids(pids)
        await self.verify_problems(pids)
        tid = await contest.add(self.domain_id, document.TYPE_CONTEST, title, content, self.user['_id'],
                                rule, begin_at, end_at, pids)
        await self.hide_problems(pids)
        self.json_or_redirect(self.reverse_url('contest_detail', ctype='contest', tid=tid))

    @base.require_priv(builtin.PRIV_USER_PROFILE)
    @base.require_perm(builtin.PERM_EDIT_PROBLEM)
    @base.require_perm(builtin.PERM_CREATE_HOMEWORK)
    @base.route_argument
    @base.post_argument
    @base.require_csrf_token
    @base.sanitize
    async def _post_homework(self, *, ctype: str, title: str, content: str,
                             begin_at_date: str, begin_at_time: str,
                             penalty_since_date: str, penalty_since_time: str,
                             extension_days: float, limit_rate: int, penalty_rules: str,
                             pids: str, show_scoreboard: bool = False):
        penalty_rules = _parse_penalty_rules_yaml(penalty_rules)
        try:
            begin_at = datetime.datetime.strptime(begin_at_date + ' ' + begin_at_time, '%Y-%m-%d %H:%M')
            begin_at = self.timezone.localize(begin_at).astimezone(pytz.utc).replace(tzinfo=None)
        except ValueError:
            raise error.ValidationError('begin_at_date', 'begin_at_time')
        try:
            penalty_since = datetime.datetime.strptime(penalty_since_date + ' ' + penalty_since_time, '%Y-%m-%d %H:%M')
            penalty_since = self.timezone.localize(penalty_since).astimezone(pytz.utc).replace(tzinfo=None)
        except ValueError:
            raise error.ValidationError('end_at_date', 'end_at_time')
        end_at = penalty_since + datetime.timedelta(days=extension_days)
        if begin_at >= penalty_since:
            raise error.ValidationError('end_at_date', 'end_at_time')
        if penalty_since > end_at:
            raise error.ValidationError('extension_days')
        pids = _parse_pids(pids)
        await self.verify_problems(pids)
        tid = await contest.add(self.domain_id, document.TYPE_HOMEWORK, title, content, self.user['_id'],
                                constant.contest.RULE_ASSIGNMENT, begin_at, end_at, pids,
                                show_scoreboard=show_scoreboard,
                                penalty_since=penalty_since, penalty_rules=penalty_rules, limit_rate=limit_rate)
        await self.hide_problems(pids)
        self.json_or_redirect(self.reverse_url('contest_detail', ctype='homework', tid=tid))


@app.route('/{ctype:contest|homework}/{tid}/edit', 'contest_edit')
class ContestEditHandler(ContestMixin, ContestPageCategoryMixin, base.Handler):
    @base.route_argument
    async def get(self, *, ctype: str, **kwargs):
        if ctype == 'homework':
            await self._get_homework(**kwargs)
        elif ctype == 'contest':
            await self._get_contest(**kwargs)
        else:
            raise error.InvalidArgumentError('ctype')

    @base.require_priv(builtin.PRIV_USER_PROFILE)
    @base.sanitize
    async def _get_contest(self, *, tid: objectid.ObjectId):
        tdoc = await contest.get(self.domain_id, document.TYPE_CONTEST, tid)
        if not self.own(tdoc, builtin.PERM_EDIT_CONTEST_SELF):
            self.check_perm(builtin.PERM_EDIT_CONTEST)
        dt = pytz.utc.localize(tdoc['begin_at']).astimezone(self.timezone)
        duration = (tdoc['end_at'] - tdoc['begin_at']).total_seconds() / 3600
        page_title = self.translate('page.contest_edit.contest.title')
        path_components = self.build_path(
            (self.translate('page.contest_main.contest.title'), self.reverse_url('contest_main', ctype='contest')),
            (tdoc['title'], self.reverse_url('contest_detail', ctype='contest', tid=tdoc['doc_id'])),
            (page_title, None))
        self.render('contest_edit.html', tdoc=tdoc,
                    date_text=dt.strftime('%Y-%m-%d'),
                    time_text=dt.strftime('%H:%M'),
                    duration=duration,
                    pids=_format_pids(tdoc['pids']),
                    page_title=page_title, path_components=path_components)

    @base.require_priv(builtin.PRIV_USER_PROFILE)
    @base.sanitize
    async def _get_homework(self, *, tid: objectid.ObjectId):
        tdoc = await contest.get(self.domain_id, document.TYPE_HOMEWORK, tid)
        if not self.own(tdoc, builtin.PERM_EDIT_HOMEWORK_SELF):
            self.check_perm(builtin.PERM_EDIT_HOMEWORK)
        begin_at = pytz.utc.localize(tdoc['begin_at']).astimezone(self.timezone)
        penalty_since = pytz.utc.localize(tdoc['penalty_since']).astimezone(self.timezone)
        end_at = pytz.utc.localize(tdoc['end_at']).astimezone(self.timezone)
        extension_days = round((end_at - penalty_since).total_seconds() / 60 / 60 / 24, ndigits=2)
        limit_rate = tdoc.get('limit_rate') or 0
        page_title = self.translate('page.contest_edit.homework.title')
        path_components = self.build_path(
            (self.translate('page.contest_main.homework.title'), self.reverse_url('contest_main', ctype='homework')),
            (tdoc['title'], self.reverse_url('contest_detail', ctype='homework', tid=tdoc['doc_id'])),
            (page_title, None))
        self.render('homework_edit.html', tdoc=tdoc,
                    date_begin_text=begin_at.strftime('%Y-%m-%d'),
                    time_begin_text=begin_at.strftime('%H:%M'),
                    date_penalty_text=penalty_since.strftime('%Y-%m-%d'),
                    time_penalty_text=penalty_since.strftime('%H:%M'),
                    extension_days=extension_days,
                    limit_rate=limit_rate,
                    penalty_rules=_format_penalty_rules_yaml(tdoc['penalty_rules']),
                    pids=_format_pids(tdoc['pids']),
                    page_title=page_title, path_components=path_components)

    @base.route_argument
    async def post(self, *, ctype: str, **kwargs):
        if ctype == 'homework':
            await self._post_homework()
        elif ctype == 'contest':
            await self._post_contest()
        else:
            raise error.InvalidArgumentError('ctype')

    @base.require_priv(builtin.PRIV_USER_PROFILE)
    @base.require_perm(builtin.PERM_EDIT_PROBLEM)
    @base.route_argument
    @base.post_argument
    @base.require_csrf_token
    @base.sanitize
    async def _post_contest(self, *, ctype: str, tid: objectid.ObjectId, title: str, content: str, rule: int,
                            begin_at_date: str = None, begin_at_time: str = None, duration: float,
                            pids: str):
        tdoc = await contest.get(self.domain_id, document.TYPE_CONTEST, tid)
        if not self.own(tdoc, builtin.PERM_EDIT_CONTEST_SELF):
            self.check_perm(builtin.PERM_EDIT_CONTEST)
        try:
            begin_at = datetime.datetime.strptime(begin_at_date + ' ' + begin_at_time, '%Y-%m-%d %H:%M')
            begin_at = self.timezone.localize(begin_at).astimezone(pytz.utc).replace(tzinfo=None)
        except ValueError:
            raise error.ValidationError('begin_at_date', 'begin_at_time')
        end_at = begin_at + datetime.timedelta(hours=duration)
        if begin_at >= end_at:
            raise error.ValidationError('duration')
        pids = _parse_pids(pids)
        await self.verify_problems(pids)
        await contest.edit(self.domain_id, document.TYPE_CONTEST, tdoc['doc_id'], title=title, content=content,
                           rule=rule, begin_at=begin_at, end_at=end_at, pids=pids)
        await self.hide_problems(pids)
        if tdoc['begin_at'] != begin_at \
                or tdoc['end_at'] != end_at \
                or set(tdoc['pids']) != set(pids) \
                or tdoc['rule'] != rule:
            await contest.recalc_status(self.domain_id, document.TYPE_CONTEST, tdoc['doc_id'])
        self.json_or_redirect(self.reverse_url('contest_detail', ctype='contest', tid=tdoc['doc_id']))

    @base.require_priv(builtin.PRIV_USER_PROFILE)
    @base.require_perm(builtin.PERM_EDIT_PROBLEM)
    @base.route_argument
    @base.post_argument
    @base.require_csrf_token
    @base.sanitize
    async def _post_homework(self, *, ctype: str, tid: objectid.ObjectId, title: str, content: str,
                             begin_at_date: str, begin_at_time: str,
                             penalty_since_date: str, penalty_since_time: str,
                             extension_days: float, limit_rate: int, penalty_rules: str,
                             pids: str, show_scoreboard: bool = False):
        tdoc = await contest.get(self.domain_id, document.TYPE_HOMEWORK, tid)
        if not self.own(tdoc, builtin.PERM_EDIT_HOMEWORK_SELF):
            self.check_perm(builtin.PERM_EDIT_HOMEWORK)
        penalty_rules = _parse_penalty_rules_yaml(penalty_rules)
        try:
            begin_at = datetime.datetime.strptime(begin_at_date + ' ' + begin_at_time, '%Y-%m-%d %H:%M')
            begin_at = self.timezone.localize(begin_at).astimezone(pytz.utc).replace(tzinfo=None)
        except ValueError:
            raise error.ValidationError('begin_at_date', 'begin_at_time')
        try:
            penalty_since = datetime.datetime.strptime(penalty_since_date + ' ' + penalty_since_time, '%Y-%m-%d %H:%M')
            penalty_since = self.timezone.localize(penalty_since).astimezone(pytz.utc).replace(tzinfo=None)
        except ValueError:
            raise error.ValidationError('end_at_date', 'end_at_time')
        end_at = penalty_since + datetime.timedelta(days=extension_days)
        if begin_at >= penalty_since:
            raise error.ValidationError('end_at_date', 'end_at_time')
        if penalty_since > end_at:
            raise error.ValidationError('extension_days')
        pids = _parse_pids(pids)
        await self.verify_problems(pids)
        await contest.edit(self.domain_id, document.TYPE_HOMEWORK, tdoc['doc_id'], title=title, content=content,
                           begin_at=begin_at, end_at=end_at, pids=pids, show_scoreboard=show_scoreboard,
                           penalty_since=penalty_since, penalty_rules=penalty_rules, limit_rate=limit_rate)
        await self.hide_problems(pids)
        if tdoc['begin_at'] != begin_at \
                or tdoc['end_at'] != end_at \
                or tdoc['penalty_since'] != penalty_since \
                or set(tdoc['pids']) != set(pids):
            await contest.recalc_status(self.domain_id, document.TYPE_HOMEWORK, tdoc['doc_id'])
        self.json_or_redirect(self.reverse_url('contest_detail', ctype='homework', tid=tid))


@app.route('/{ctype:contest|homework}/{tid}/system_test', 'contest_system_test')
class ContestSystemTestHandler(ContestMixin, ContestPageCategoryMixin, base.Handler):
    @base.route_argument
    async def get(self, *, ctype: str, **kwargs):
        if ctype == 'homework':
            await self._get_homework(**kwargs)
        elif ctype == 'contest':
            raise error.InvalidArgumentError('ctype')
            # await self._get_contest(**kwargs)
        else:
            raise error.InvalidArgumentError('ctype')

    @base.require_priv(builtin.PRIV_USER_PROFILE)
    @base.sanitize
    async def _get_homework(self, *, tid: objectid.ObjectId):
        tdoc = await contest.get(self.domain_id, document.TYPE_HOMEWORK, tid)
        if not self.own(tdoc, builtin.PERM_EDIT_HOMEWORK_SELF):
            self.check_perm(builtin.PERM_EDIT_HOMEWORK)
        page_title = self.translate('page.contest_system_test.homework.title')
        path_components = self.build_path(
            (self.translate('page.contest_main.homework.title'), self.reverse_url('contest_main', ctype='homework')),
            (tdoc['title'], self.reverse_url('contest_detail', ctype='homework', tid=tdoc['doc_id'])),
            (page_title, None))
        # print(tdoc)
        self.render('homework_system_test.html', tdoc=tdoc, moss_user_id=options.moss_user_id,
                    page_title=page_title, path_components=path_components)

    @base.route_argument
    async def post(self, *, ctype: str, **kwargs):
        if ctype == 'homework':
            await self._post_homework()
        elif ctype == 'contest':
            raise error.InvalidArgumentError('ctype')
            # await self._post_contest()
        else:
            raise error.InvalidArgumentError('ctype')

    def split_tags(self, s):
        s = s.replace('，', ',')  # Chinese ', '
        return sorted(list(filter(lambda _: _ != '', map(lambda _: _.strip(), s.split(',')))))

    @base.require_priv(builtin.PRIV_USER_PROFILE)
    @base.require_perm(builtin.PERM_EDIT_PROBLEM)
    @base.route_argument
    @base.post_argument
    @base.require_csrf_token
    @base.sanitize
    async def _post_homework(self, *, ctype: str, tid: objectid.ObjectId, judge_category: str,
                             system_test_new: bool = False):
        tdoc = await contest.get(self.domain_id, document.TYPE_HOMEWORK, tid)
        if not self.own(tdoc, builtin.PERM_EDIT_HOMEWORK_SELF):
            self.check_perm(builtin.PERM_EDIT_HOMEWORK)
        doc_type = constant.contest.CTYPE_TO_DOCTYPE[ctype]
        judge_category = self.split_tags(judge_category)
        tdoc, tsdocs = await contest.get_and_list_status(self.domain_id, doc_type, tid)
        pid_num = len(tdoc['pids'])

        for tsdoc in tsdocs:
            # continue if no record found
            if not tsdoc.get('journal'):
                continue

            rids = list(map(lambda x: x['rid'], tsdoc['journal']))
            rdocs = record.get_multi(get_hidden=True, _id={'$in': rids}).sort([('_id', -1)])

            # find the newest record to be system tested
            pid_set = set()
            async for rdoc in rdocs:
                if len(pid_set) >= pid_num:
                    # all tested
                    break
                if rdoc['pid'] in pid_set:
                    continue
                if system_test_new and sorted(rdoc['judge_category']) == judge_category:
                    # in system test new mode, records with same judge_category are skipped
                    pid_set.add(rdoc['pid'])
                    continue
                if len(rdoc['judge_category']) > 0:
                    # records with judge_category are not origin records
                    continue
                rid = await record.system_test(rdoc, judge_category)
                await contest.update_status(self.domain_id, rdoc['tid'], rdoc['uid'], rid, rdoc['pid'], False, 0)
                pid_set.add(rdoc['pid'])

        self.json_or_redirect(self.reverse_url('contest_detail', ctype=ctype, tid=tid))


@app.route('/{ctype:contest|homework}/{tid}/export', 'contest_export')
class ContestExporttHandler(ContestMixin, base.Handler):
    @base.route_argument
    async def post(self, *, ctype: str, **kwargs):
        if ctype == 'homework':
            await self._post_homework()
        elif ctype == 'contest':
            raise error.InvalidArgumentError('ctype')
            # await self._post_contest()
        else:
            raise error.InvalidArgumentError('ctype')

    @base.require_priv(builtin.PRIV_USER_PROFILE)
    @base.require_perm(builtin.PERM_EDIT_PROBLEM)
    @base.route_argument
    @base.post_argument
    @base.require_csrf_token
    @base.sanitize
    async def _post_homework(self, *, ctype: str, tid: objectid.ObjectId):
        tdoc = await contest.get(self.domain_id, document.TYPE_HOMEWORK, tid)
        if not self.own(tdoc, builtin.PERM_EDIT_HOMEWORK_SELF):
            self.check_perm(builtin.PERM_EDIT_HOMEWORK)
        doc_type = constant.contest.CTYPE_TO_DOCTYPE[ctype]

        rdocs = await self.get_latest_records(doc_type, tid)
        data = await contest.export_records(rdocs)
        await self.binary(data, file_name='{}_records.zip'.format(tid))


@app.route('/{ctype:contest|homework}/{tid}/moss', 'contest_moss')
class ContestMosstHandler(ContestMixin, base.Handler):
    def split_tags(self, s):
        s = s.replace('，', ',')  # Chinese ', '
        return list(filter(lambda _: _ != '', map(lambda _: _.strip(), s.split(','))))

    @base.route_argument
    async def post(self, *, ctype: str, **kwargs):
        if ctype == 'homework':
            await self._post_homework()
        elif ctype == 'contest':
            raise error.InvalidArgumentError('ctype')
            # await self._post_contest()
        else:
            raise error.InvalidArgumentError('ctype')

    @base.require_priv(builtin.PRIV_USER_PROFILE)
    @base.require_perm(builtin.PERM_EDIT_PROBLEM)
    @base.route_argument
    @base.post_argument
    @base.require_csrf_token
    @base.sanitize
    async def _post_homework(self, *, ctype: str, tid: objectid.ObjectId, language: str, wildcards: str,
                             ignore_limit: int = 10):
        async def moss_task():
            moss_url = await moss.moss_test(rdocs, language=language, wildcards=wildcards, ignore_limit=ignore_limit)
            if moss_url:
                _logger.info('moss url %s', moss_url)
                await contest.update_moss_result(self.domain_id, document.TYPE_HOMEWORK, tid, moss_url=moss_url)

        tdoc = await contest.get(self.domain_id, document.TYPE_HOMEWORK, tid)
        if not self.own(tdoc, builtin.PERM_EDIT_HOMEWORK_SELF):
            self.check_perm(builtin.PERM_EDIT_HOMEWORK)
        doc_type = constant.contest.CTYPE_TO_DOCTYPE[ctype]

        rdocs = await self.get_latest_records(doc_type, tid)
        wildcards = self.split_tags(wildcards)

        _logger.info('Submit Moss for %s', tid)
        asyncio.ensure_future(moss_task())

        self.json_or_redirect(self.reverse_url('contest_system_test', ctype=ctype, tid=tid))
