import asyncio
import collections
import datetime
import functools

from vj4 import app
from vj4 import constant
from vj4 import error
from vj4 import constant
from vj4.model import builtin
from vj4.model import document
from vj4.model import domain
from vj4.model import user
from vj4.model.adaptor import discussion
from vj4.model.adaptor import contest
from vj4.model.adaptor import training
from vj4.handler import base
from vj4.handler import training as training_handler
from vj4.util import validator


@app.route('/', 'domain_main')
class DomainMainHandler(training_handler.TrainingStatusMixin, base.Handler):
  CONTESTS_ON_MAIN = 5
  HOMEWORKS_ON_MAIN = 5
  TRAININGS_ON_MAIN = 5
  DISCUSSIONS_ON_MAIN = 20

  async def prepare_domain(self):
    dudict = await domain.get_dict_user_by_domain_id(self.user['_id'])
    dids = list(dudict.keys())
    # dodocs = await domain.get_multi(_id={'$in': dids}).to_list()
    dodocs = await domain.get_multi().to_list()
    can_manage = {}
    for dodoc in builtin.DOMAINS + dodocs:
      role = dudict.get(dodoc['_id'], {}).get('role', builtin.ROLE_DEFAULT)
      mask = domain.get_all_roles(dodoc).get(role, builtin.PERM_NONE)
      can_manage[dodoc['_id']] = (
          ((builtin.PERM_EDIT_DESCRIPTION | builtin.PERM_EDIT_PERM) & mask) != 0
          or self.has_priv(builtin.PRIV_MANAGE_ALL_DOMAIN))
    return dodocs, dudict, can_manage

  async def prepare_contest(self):
    if self.has_perm(builtin.PERM_VIEW_CONTEST):
      docs = await contest.get_multi(self.domain_id, document.TYPE_CONTEST) \
                           .limit(self.CONTESTS_ON_MAIN) \
                           .to_list()
      dict = await contest.get_dict_status(self.domain_id, self.user['_id'],
                                             (doc['doc_id'] for doc in docs))
    else:
      docs = []
      dict = {}
    return docs, dict

  async def prepare_homework(self):
    if self.has_perm(builtin.PERM_VIEW_HOMEWORK):
      docs = await contest.get_multi(self.domain_id, document.TYPE_HOMEWORK) \
                           .limit(self.HOMEWORKS_ON_MAIN) \
                           .to_list()
      dict = await contest.get_dict_status(self.domain_id, self.user['_id'],
                                             (doc['doc_id'] for doc in docs))
    else:
      docs = []
      dict = {}
    return docs, dict

  async def prepare_training(self):
    if self.has_perm(builtin.PERM_VIEW_TRAINING):
      docs = await training.get_multi(self.domain_id) \
                            .sort('doc_id', 1) \
                            .limit(self.TRAININGS_ON_MAIN) \
                            .to_list()
      dict = await training.get_dict_status(self.domain_id, self.user['_id'],
                                              (doc['doc_id'] for doc in docs))
    else:
      docs = []
      dict = {}
    return docs, dict

  async def prepare_discussion(self):
    if self.has_perm(builtin.PERM_VIEW_DISCUSSION):
      docs = await discussion.get_multi(self.domain_id) \
                              .limit(self.DISCUSSIONS_ON_MAIN) \
                              .to_list()
      dict = await discussion.get_dict_vnodes(self.domain_id, map(discussion.node_id, docs))
    else:
      docs = []
      dict = {}
    return docs, dict

  async def get(self):
    (dodocs, dudict, can_manage), (tdocs, tsdict), (hwdocs, hwdict), (trdocs, trsdict), (ddocs, vndict) \
          = await asyncio.gather(
            self.prepare_domain(),
            self.prepare_contest(),
            self.prepare_homework(),
            self.prepare_training(),
            self.prepare_discussion())
    udict = await user.get_dict(ddoc['owner_uid'] for ddoc in ddocs)
    self.render('domain_main.html',
                discussion_nodes=await discussion.get_nodes(self.domain_id),
                can_manage=can_manage,
                dodocs=dodocs, dudict=dudict,
                tdocs=tdocs, tsdict=tsdict,
                hwdocs=hwdocs, hwdict=hwdict,
                trdocs=trdocs, trsdict=trsdict,
                ddocs=ddocs, vndict=vndict,
                udict=udict, datetime_stamp=self.datetime_stamp)


@app.route('/domain', 'domain_manage')
class DomainManageHandler(base.Handler):
  async def get(self):
    self.redirect(self.reverse_url('domain_manage_dashboard'))


@app.route('/domain/dashboard', 'domain_manage_dashboard')
class DomainDashboardHandler(base.Handler):
  async def get(self):
    if not self.has_perm(builtin.PERM_EDIT_PERM):
       self.check_perm(builtin.PERM_EDIT_DESCRIPTION)
    self.render('domain_manage_dashboard.html')


@app.route('/domain/edit', 'domain_manage_edit')
class DomainEditHandler(base.Handler):
  @base.require_perm(builtin.PERM_EDIT_DESCRIPTION)
  async def get(self):
    self.render('domain_manage_edit.html')

  @base.require_perm(builtin.PERM_EDIT_DESCRIPTION)
  @base.post_argument
  @base.require_csrf_token
  @base.sanitize
  async def post(self, *, name: str, gravatar: str, bulletin: str):
    await domain.edit(self.domain_id, name=name, gravatar=gravatar, bulletin=bulletin)
    self.json_or_redirect(self.url)


@app.route('/domain/join_applications', 'domain_manage_join_applications')
class DomainJoinApplicationsHandler(base.Handler):
  @property
  @functools.lru_cache()
  def now(self):
    # TODO(twd2): This does not work on multi-machine environment.
    return datetime.datetime.utcnow()

  @base.require_perm(builtin.PERM_EDIT_PERM)
  async def get(self):
    roles = sorted(list(self.domain['roles'].keys()))
    roles_with_text = [(role, role) for role in roles]
    join_settings = domain.get_join_settings(self.domain, self.now)
    expirations = constant.domain.JOIN_EXPIRATION_RANGE.copy()
    if not join_settings:
      del expirations[constant.domain.JOIN_EXPIRATION_KEEP_CURRENT]
    self.render('domain_manage_join_applications.html', roles_with_text=roles_with_text,
                join_settings=join_settings, expirations=expirations)

  @base.require_perm(builtin.PERM_EDIT_PERM)
  @base.post_argument
  @base.require_csrf_token
  @base.sanitize
  async def post(self, *, method: int, role: str=None, expire: int=None,
                 invitation_code: str=''):
    current_join_settings = domain.get_join_settings(self.domain, self.now)
    if method not in constant.domain.JOIN_METHOD_RANGE:
      raise error.ValidationError('method')
    if method == constant.domain.JOIN_METHOD_NONE:
      join_settings = None
    else:
      if role not in self.domain['roles']:
        raise error.ValidationError('role')
      if expire not in constant.domain.JOIN_EXPIRATION_RANGE:
        raise error.ValidationError('expire')
      if not current_join_settings and expire == constant.domain.JOIN_EXPIRATION_KEEP_CURRENT:
        raise error.ValidationError('expire')
      if method == constant.domain.JOIN_METHOD_CODE:
        validator.check_domain_invitation_code(invitation_code)
      join_settings={'method': method, 'role': role}
      if method == constant.domain.JOIN_METHOD_CODE:
        join_settings['code'] = invitation_code
      if expire == constant.domain.JOIN_EXPIRATION_KEEP_CURRENT:
        join_settings['expire'] = current_join_settings['expire']
      elif expire == constant.domain.JOIN_EXPIRATION_UNLIMITED:
        join_settings['expire'] = None
      else:
        join_settings['expire'] = self.now + datetime.timedelta(hours=expire)
    await domain.edit(self.domain_id, join=join_settings)
    self.json_or_redirect(self.referer_or_main)


@app.route('/join', 'domain_join', global_route=True)
class DomainJoinHandler(base.Handler):
  @property
  @functools.lru_cache()
  def now(self):
    # TODO(twd2): This does not work on multi-machine environment.
    return datetime.datetime.utcnow()

  async def ensure_user_not_member(self):
    dudoc = await domain.get_user(self.domain_id, self.user['_id'])
    if dudoc and 'role' in dudoc:
      raise error.DomainJoinAlreadyMemberError(self.domain_id, self.user['_id'])

  @base.require_priv(builtin.PRIV_USER_PROFILE)
  @base.get_argument
  @base.sanitize
  async def get(self, *, code: str=''):
    join_settings = domain.get_join_settings(self.domain, self.now)
    if not join_settings:
      raise error.DomainJoinForbiddenError(self.domain_id)
    await self.ensure_user_not_member()
    self.render('domain_join.html', join_settings=join_settings, code=code)

  @base.require_priv(builtin.PRIV_USER_PROFILE)
  @base.post_argument
  @base.require_csrf_token
  @base.sanitize
  async def post(self, *, code: str=''):
    join_settings = domain.get_join_settings(self.domain, self.now)
    if not join_settings:
      raise error.DomainJoinForbiddenError(self.domain_id)
    await self.ensure_user_not_member()
    if join_settings['method'] == constant.domain.JOIN_METHOD_CODE:
      if join_settings['code'] != code:
        raise error.InvalidJoinInvitationCodeError(self.domain_id)
    try:
      await domain.add_user_role(self.domain_id, self.user['_id'], join_settings['role'])
    except error.UserAlreadyDomainMemberError:
      raise error.DomainJoinAlreadyMemberError(self.domain_id, self.user['_id']) from None
    self.json_or_redirect(self.reverse_url('domain_main'))


@app.route('/domain/discussion', 'domain_manage_discussion')
class DomainEditHandler(base.Handler):
  @base.require_perm(builtin.PERM_EDIT_DESCRIPTION)
  async def get(self):
    self.render('domain_manage_discussion.html',
                discussion_nodes=await discussion.get_nodes(self.domain_id))

  @base.require_perm(builtin.PERM_EDIT_DESCRIPTION)
  @base.post_argument
  @base.require_csrf_token
  @base.sanitize
  async def post(self, *kwargs):
    await discussion.initialize(self.domain_id)
    self.json_or_redirect(self.url)


@app.route('/domain/user', 'domain_manage_user')
class DomainUserHandler(base.OperationHandler):
  @base.require_perm(builtin.PERM_EDIT_PERM)
  async def get(self):
    uids = []
    rudocs = collections.defaultdict(list)
    async for dudoc in domain.get_multi_user(domain_id=self.domain_id,
                                             role={'$gte': ''},
                                             fields={'uid': 1, 'role': 1}):
      if 'role' in dudoc:
        uids.append(dudoc['uid'])
        rudocs[dudoc['role']].append(dudoc)
    roles = sorted(list(domain.get_all_roles(self.domain).keys()))
    roles_with_text = [(role, role) for role in roles]
    udict = await user.get_dict(uids)
    self.render('domain_manage_user.html', roles=roles, roles_with_text=roles_with_text,
                rudocs=rudocs, udict=udict)


  @base.require_perm(builtin.PERM_EDIT_PERM)
  @base.require_csrf_token
  @base.sanitize
  async def post_add_user(self, *, uid: int, role: str):
    await domain.add_user_role(self.domain_id, uid, role)
    self.json_or_redirect(self.url)


  @base.require_perm(builtin.PERM_EDIT_PERM)
  @base.require_csrf_token
  @base.sanitize
  async def post_set_user(self, *, uid: int, role: str):
    if role:
      await domain.set_user_role(self.domain_id, uid, role)
    else:
      await domain.unset_user_role(self.domain_id, uid)
    self.json_or_redirect(self.url)


  @base.require_perm(builtin.PERM_EDIT_PERM)
  @base.require_csrf_token
  @base.sanitize
  async def post_set_users(self, *, uid: int, role: str=None):
    try:
      uids = map(int, (await self.request.post()).getall('uid'))
    except ValueError:
      raise error.ValidationError('uid')
    if role:
      # user must exist.
      await domain.set_users_role(self.domain_id, uids, role)
    else:
      await domain.unset_users_role(self.domain_id, uids)
    self.json_or_redirect(self.url)


@app.route('/domain/permission', 'domain_manage_permission')
class DomainPermissionHandler(base.Handler):
  @base.require_perm(builtin.PERM_EDIT_PERM)
  async def get(self):
    def bitand(a, b):
      return a & b
    # unmodifiable roles are not visible in UI so that we are not using get_all_roles() here
    roles = sorted(list(self.domain['roles'].keys()))
    self.render('domain_manage_permission.html', bitand=bitand, roles=roles)

  @base.require_perm(builtin.PERM_EDIT_PERM)
  @base.post_argument
  @base.require_csrf_token
  async def post(self, **kwargs):
    roles = dict()
    # unmodifiable roles are not modifiable so that we are not using get_all_roles() here
    for role in self.domain['roles']:
      perms = 0
      for perm in (await self.request.post()).getall(role, []):
       perm = int(perm)
       if perm in builtin.PERMS_BY_KEY:
          perms |= perm
      roles[role] = perms
    await domain.set_roles(self.domain_id, roles)
    self.json_or_redirect(self.url)


@app.route('/domain/role', 'domain_manage_role')
class DomainRoleHandler(base.OperationHandler):
  @base.require_perm(builtin.PERM_EDIT_PERM)
  async def get(self):
    rucounts = collections.defaultdict(int)
    async for dudoc in domain.get_multi_user(domain_id=self.domain_id,
                                             role={'$gte': ''},
                                             fields={'uid': 1, 'role': 1}):
      if 'role' in dudoc:
        rucounts[dudoc['role']] += 1
    # built-in roles are displayed additionally so that we don't need to use get_all_roles() here
    roles = sorted(list(self.domain['roles'].keys()))
    self.render('domain_manage_role.html', rucounts=rucounts, roles=roles)

  @base.require_perm(builtin.PERM_EDIT_PERM)
  @base.require_csrf_token
  @base.sanitize
  async def post_add(self, *, role: str):
    if role in domain.get_all_roles(self.domain):
      raise error.DomainRoleAlreadyExistError(self.domain_id, role)
    await domain.set_role(self.domain_id, role, builtin.DEFAULT_PERMISSIONS)
    self.json_or_redirect(self.url)

  @base.require_perm(builtin.PERM_EDIT_PERM)
  @base.require_csrf_token
  @base.sanitize
  async def post_delete(self, *, role: str):
    await domain.delete_roles(self.domain_id, (await self.request.post()).getall('role'))
    self.json_or_redirect(self.url)
