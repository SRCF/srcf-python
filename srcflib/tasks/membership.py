"""
Creation and management of member and society accounts.
"""

from datetime import datetime
from enum import Enum, auto
import logging
import os
from typing import Optional, Set, Tuple

from sqlalchemy.orm import Session as SQLASession

from srcf.database import MailHandler, Member, Society
from srcf.database.queries import get_member, get_society
from srcf.mail import SYSADMINS

from ..email import send
from ..plumbing import bespoke, pgsql as pgsql_p, unix
from ..plumbing.common import Collect, Password, Result, State, owner_home
from . import mailman, mysql, pgsql


LOG = logging.getLogger(__name__)

MEMBER_LOG = "/var/log/admin/users.log"
SOCIETY_LOG = "/var/log/admin/socs.log"


class RemoveProcess(Enum):
    """
    Context of the removal of a user from a group's admins.
    """

    DEFAULT = auto()
    """An explicit removal request."""
    USER_CANCEL = auto()
    """The user's personal account is being cancelled, removing them from all of their groups."""
    GROUP_DELETE = auto()
    """The group is being deleted, removing all admins from it."""


@Result.collect_value
def create_member(sess: SQLASession, crsid: str, preferred_name: str, surname: str,
                  email: str, mail_handler: MailHandler, is_member: bool = True,
                  is_user: bool = True, is_contactable: bool = True, social: bool = False,
                  new_passwd: bool = False) -> Collect[Tuple[Member, Optional[Password]]]:
    """
    Register and provision a new member of the SRCF.
    """
    res_record = yield from bespoke.ensure_member(sess, crsid, preferred_name, surname, email,
                                                  mail_handler, is_member, is_user, is_contactable)
    member = res_record.value
    yield unix.ensure_group(crsid, gid=member.gid, system=True)
    res_user = yield from unix.ensure_user(crsid, uid=member.uid, system=True, gid=member.gid,
                                           home_dir=os.path.join("/home", crsid),
                                           real_name=member.name)
    new_user = res_user.state == State.created
    user = res_user.value
    passwd = None
    if new_user or new_passwd:
        res_passwd = yield from unix.reset_password(user)
        passwd = res_passwd.value
    yield bespoke.update_nis(new_user)
    yield unix.create_home(user, owner_home(member))
    yield unix.create_home(user, owner_home(member, True), True)
    yield bespoke.populate_home_dir(member)
    yield bespoke.set_home_exim_acl(member)
    yield bespoke.create_public_html(member)
    if res_record:
        yield bespoke.update_quotas()
    if mail_handler == MailHandler.pip:
        yield bespoke.create_forwarding_file(member)
    yield bespoke.create_legacy_mailbox(member)
    if new_user:
        yield bespoke.queue_list_subscription(member, "maintenance")
        if social:
            yield bespoke.queue_list_subscription(member, "social")
    if res_record:
        yield bespoke.export_members()
    if passwd:
        yield bespoke.log_to_file(MEMBER_LOG, "{} user account created".format(crsid))
        yield send(member, "tasks/member_create.j2", {"password": passwd})
    return (member, passwd)


@Result.collect_value
def create_sysadmin(sess: SQLASession, member: Member,
                    new_passwd: bool = False) -> Collect[Optional[Password]]:
    """
    Create an administrative account for an existing member.
    """
    if not member.user:
        raise ValueError("{!r} is not an active user")
    username = "{}-adm".format(member.crsid)
    real_name = "{} (Sysadmin Account)".format(member.name)
    res_group = yield from unix.ensure_group(username)
    group = res_group.value
    res_user = yield from unix.ensure_user(username, gid=group.gr_gid, real_name=real_name)
    new_user = res_user.state == State.created
    user = res_user.value
    if new_user or new_passwd:
        res_passwd = yield from unix.reset_password(user)
        passwd = res_passwd.value
    else:
        passwd = None
    yield bespoke.update_nis(new_user)
    yield unix.create_home(user, os.path.join("/home", username))
    yield unix.create_home(user, os.path.join("/public/home", username), True)
    yield bespoke.populate_home_dir(user)
    yield unix.add_to_group(user, unix.get_group("sysadmins"))
    yield unix.add_to_group(user, unix.get_group("adm"))
    yield unix.grant_netgroup(user, "sysadmins")
    for soc in ("executive", "srcf-admin", "srcf-web"):
        yield add_society_admin(sess, member, get_society(soc, sess))
    with pgsql.context() as cursor:
        yield pgsql_p.ensure_user(cursor, username)
        yield pgsql_p.grant_role(cursor, username, pgsql_p.get_role(cursor, "sysadmins"))
    if passwd:
        yield bespoke.log_to_file(MEMBER_LOG, "{} administrative account created".format(username))
        yield send(member, "tasks/sysadmin_create.j2", {"username": username, "password": passwd})
        yield send(SYSADMINS, "tasks/sysadmin_create_log.j2", {"username": username})
    return passwd


@Result.collect_value
def reset_password(member: Member) -> Collect[Password]:
    """
    Reset the password of a member's shell account.
    """
    user = unix.get_user(member.uid)
    res_passwd = yield from unix.reset_password(user)
    passwd = res_passwd.value
    yield bespoke.update_nis()
    yield bespoke.log_to_file(MEMBER_LOG, "{} password reset".format(member.crsid))
    yield send(member, "/tasks/member_password.j2", {"password": passwd})
    return passwd


@Result.collect_value
def update_member_name(sess: SQLASession, member: Member,
                       preferred_name: str, surname: str) -> Collect[Member]:
    """
    Update a member's registered name.
    """
    res_record = yield from bespoke.ensure_member(sess=sess, crsid=member.crsid,
                                                  preferred_name=preferred_name,
                                                  surname=surname,
                                                  email=member.email,
                                                  mail_handler=MailHandler[member.mail_handler],
                                                  is_member=member.member,
                                                  is_user=member.user,
                                                  is_contactable=member.contactable)
    member = res_record.value
    user = unix.get_user(member.uid)
    res_name = yield from unix.set_real_name(user, member.name)
    yield bespoke.update_nis()
    if res_name:
        yield send(member, "tasks/member_rename.j2")
    return member


@Result.collect
def _sync_society_admins(sess: SQLASession, society: Society, admins: Set[str],
                         process: RemoveProcess = RemoveProcess.DEFAULT) -> Collect[None]:
    society = get_society(society.society, sess)
    if society.admin_crsids == admins:
        return
    group = unix.get_group(society.gid)
    for crsid in admins - society.admin_crsids:
        member = get_member(crsid, sess)
        yield bespoke.add_society_admin(sess, member, society, group)
    for crsid in society.admin_crsids - admins:
        member = get_member(crsid, sess)
        yield bespoke.remove_society_admin(sess, member, society, group)
    with mysql.context() as cursor:
        yield mysql.sync_society_roles(cursor, society)
    with pgsql.context() as cursor:
        yield pgsql.sync_society_roles(cursor, society)


@Result.collect_value
def create_society(sess: SQLASession, name: str, description: str, admins: Set[str],
                   role_email: Optional[str] = None) -> Collect[Society]:
    """
    Register a new SRCF society account.
    """
    res_record = yield from bespoke.ensure_society(sess, name, description, role_email)
    society = res_record.value
    yield unix.ensure_group(name, gid=society.gid, system=True)
    res_user = yield from unix.ensure_user(name, uid=society.uid, system=True,
                                           gid=society.gid, active=False,
                                           home_dir=os.path.join("/societies", name),
                                           real_name=description)
    new_user = res_user.state == State.created
    user = res_user.value
    yield bespoke.update_nis(res_user.state == State.created)
    yield unix.create_home(user, owner_home(society))
    yield unix.create_home(user, owner_home(society, True), True)
    yield bespoke.set_home_exim_acl(society)
    yield bespoke.create_public_html(society)
    res_admins = yield _sync_society_admins(sess, society, admins)
    if res_record:
        yield bespoke.update_quotas()
    if res_admins:
        yield bespoke.generate_sudoers()
    if res_record:
        yield bespoke.export_members()
    if new_user:
        yield send(society, "tasks/society_create.j2")
    return society


@Result.collect
def add_society_admin(sess: SQLASession, member: Member, society: Society,
                      actor: Optional[Member] = None) -> Collect[None]:
    """
    Promote a member to a society account admin.
    """
    group = unix.get_group(society.gid)
    res_add = yield from bespoke.add_society_admin(sess, member, society, group)
    with mysql.context() as cursor:
        yield mysql.sync_society_roles(cursor, society)
    with pgsql.context() as cursor:
        yield pgsql.sync_society_roles(cursor, society)
    if res_add:
        yield bespoke.log_to_file(SOCIETY_LOG, "{} added to {} operator list"
                                               .format(member.crsid, society.society))
        yield send(society, "tasks/society_admin_add.j2", {"member": member, "actor": actor})
        yield send(member, "tasks/society_admin_join.j2", {"society": society, "actor": actor})


@Result.collect
def remove_society_admin(sess: SQLASession, member: Member, society: Society,
                         process: RemoveProcess = RemoveProcess.DEFAULT,
                         actor: Optional[Member] = None) -> Collect[None]:
    """
    Demote a member from a society account's list of admins.

    During user cancellation, the user is not notified of their group removal.  During group
    deletion, no emails are sent to any admins.
    """
    group = unix.get_group(society.gid)
    res_remove = yield from bespoke.remove_society_admin(sess, member, society, group)
    with mysql.context() as cursor:
        yield mysql.sync_society_roles(cursor, society)
    with pgsql.context() as cursor:
        yield pgsql.sync_society_roles(cursor, society)
    if res_remove:
        yield bespoke.log_to_file(SOCIETY_LOG, "{} removed from {} operator list"
                                               .format(member.crsid, society.society))
        context = {"actor": actor, "process": process, "RemoveProcess": RemoveProcess}
        if society.admins and process is not RemoveProcess.GROUP_DELETE:
            yield send(society, "tasks/society_admin_remove.j2", {"member": member, **context})
        if process is RemoveProcess.DEFAULT:
            yield send(member, "tasks/society_admin_leave.j2", {"society": society, **context})


@Result.collect
def cancel_member(sess: SQLASession, member: Member, is_member: Optional[bool] = None,
                  is_contactable: Optional[bool] = None, keep_groups: bool = False) -> Collect[None]:
    """
    Suspend the user account of a member.
    """
    user = unix.get_user(member.uid)
    res_user = yield from unix.enable_user(user, False)
    yield bespoke.clear_crontab(member)
    yield bespoke.slay_user(member)
    # TODO: for server in {"cavein", "doom", "sinkhole"}:
    #   bespoke.clear_crontab(member); bespoke.slay_user(member)
    yield bespoke.archive_website(member)
    if is_member is None:
        is_member = member.member
    if is_contactable is None:
        is_contactable = member.contactable
    res_member = yield from bespoke.ensure_member(sess, member.crsid,
                                                  member.preferred_name, member.surname,
                                                  member.email, MailHandler[member.mail_handler],
                                                  is_member, False, is_contactable)
    with mysql.context() as cursor:
        yield mysql.drop_account(cursor, member)
    with pgsql.context() as cursor:
        yield pgsql.disable_account(cursor, member)
    if not keep_groups:
        societies = set(member.societies)
        for society in societies:
            yield remove_society_admin(sess, member, society, RemoveProcess.USER_CANCEL)
    yield bespoke.update_nis()
    if res_user or res_member:
        yield bespoke.log_to_file(MEMBER_LOG, "{} user account cancelled".format(member.crsid))
        yield send(SYSADMINS, "tasks/member_cancel.j2", {"username": member.crsid})


@Result.collect
def reactivate_member(sess: SQLASession, member: Member, email: str,
                      new_passwd: bool = True) -> Collect[None]:
    """
    Reinstate the user account of a member, and update their contact address.
    """
    res_member = yield from bespoke.ensure_member(sess, member.crsid,
                                                  member.preferred_name, member.surname,
                                                  email, MailHandler[member.mail_handler],
                                                  True, True, True)
    user = unix.get_user(member.uid)
    res_user = yield from unix.enable_user(user, True)
    passwd = None
    if res_user or new_passwd:
        res_passwd = yield from unix.reset_password(user)
        passwd = res_passwd.value
    if res_member or res_user or passwd:
        yield bespoke.log_to_file(MEMBER_LOG, "{} user account reactivated".format(member.crsid))
        yield send(member, "tasks/member_reactivate.j2", {"password": passwd})
        yield send(SYSADMINS, "tasks/member_reactivate_log.j2", {"member": member})


@Result.collect
def cancel_sysadmin(sess: SQLASession, member: Member) -> Collect[None]:
    """
    Suspend the administrative account of a member.
    """
    username = "{}-adm".format(member.crsid)
    user = unix.get_user(username)
    res_sysadmins = yield from unix.remove_from_group(user, unix.get_group("sysadmins"))
    res_adm = yield from unix.remove_from_group(user, unix.get_group("adm"))
    yield unix.revoke_netgroup(user, "sysadmins")
    res_soc = yield from remove_society_admin(sess, member, get_society("srcf-admin", sess))
    committee = get_society("srcf", sess)
    if member.crsid not in committee.admin_crsids:
        for soc in ("executive", "srcf-web"):
            yield remove_society_admin(sess, member, get_society(soc, sess))
    res_user = yield from unix.enable_user(user, False)
    yield bespoke.clear_crontab(user)
    yield bespoke.slay_user(user)
    # TODO: for server in {"cavein", "doom", "sinkhole"}:
    #   bespoke.clear_crontab(user); bespoke.slay_user(user)
    with pgsql.context() as cursor:
        yield pgsql_p.drop_user(cursor, username)
    yield bespoke.update_nis()
    if any((res_sysadmins, res_adm, res_soc, res_user)):
        yield bespoke.log_to_file(MEMBER_LOG, "{} user account cancelled".format(username))
        yield send(SYSADMINS, "tasks/sysadmin_cancel.j2", {"username": username})


@Result.collect
def delete_member(sess: SQLASession, member: Member) -> Collect[None]:
    """
    Delete all traces of a member account.
    """
    yield cancel_member(sess, member)
    res_member = yield from bespoke.ensure_member(sess, member.crsid, None, None, None,
                                                  MailHandler[member.mail_handler], False, False,
                                                  member.contactable)
    member = res_member.value
    note = "User account erased: {}".format(datetime.now().strftime("%Y-%m-%d %H:%M"))
    if member.notes:
        member.notes = "{}\n{}".format(member.notes, note)
    else:
        member.notes = note
    yield bespoke.scrub_member_jobs(sess, member)
    with mysql.context() as cursor:
        yield mysql.drop_all_databases(cursor, member)
    with pgsql.context() as cursor:
        yield pgsql.drop_all_databases(cursor, member)
        yield pgsql.drop_account(cursor, member)
    for mlist in mailman.get_list_suffixes(member):
        yield mailman.remove_list(member, mlist, True)
    for domain in bespoke.get_custom_domains(sess, member):
        yield bespoke.remove_custom_domain(sess, member, domain.domain)
    yield bespoke.empty_legacy_mailbox(member)
    # TODO: hades mail
    yield bespoke.scrub_user(member)
    yield bespoke.scrub_group(member)
    yield bespoke.update_nis()
    yield bespoke.delete_files(member)
    yield bespoke.log_to_file(MEMBER_LOG, "{} user account deleted".format(member.crsid))
    yield send(SYSADMINS, "tasks/member_delete.j2", {"member": member})


@Result.collect
def delete_society(sess: SQLASession, society: Society) -> Collect[None]:
    """
    Archive and delete all traces of a society account.
    """
    yield _sync_society_admins(sess, society, set(), RemoveProcess.GROUP_DELETE)
    yield bespoke.clear_crontab(society)
    yield bespoke.slay_user(society)
    # TODO: for server in {"cavein", "doom", "sinkhole"}:
    #   bespoke.clear_crontab(society); bespoke.slay_user(society)
    yield bespoke.archive_society_files(society)
    yield bespoke.delete_files(society)
    with mysql.context() as cursor:
        yield mysql.drop_all_databases(cursor, society)
        yield mysql.drop_account(cursor, society)
    with pgsql.context() as cursor:
        yield pgsql.drop_all_databases(cursor, society)
        yield pgsql.drop_account(cursor, society)
    for mlist in mailman.get_list_suffixes(society):
        yield mailman.remove_list(society, mlist)
    for domain in bespoke.get_custom_domains(sess, society):
        yield bespoke.remove_custom_domain(sess, society, domain.domain)
    yield bespoke.scrub_user(society)
    yield bespoke.scrub_group(society)
    yield bespoke.update_nis()
    yield bespoke.delete_society(sess, society)
    yield bespoke.export_members()
    yield bespoke.log_to_file(SOCIETY_LOG, "{} group account deleted".format(society.society))
    yield send(SYSADMINS, "tasks/society_delete.j2", {"society": society})


@Result.collect_value
def update_society_description(sess: SQLASession, society: Society,
                               description: str) -> Collect[Society]:
    """
    Update a society's description ('full name').
    """
    res_record = yield from bespoke.ensure_society(sess=sess, name=society.society,
                                                   description=description,
                                                   role_email=society.role_email)
    society = res_record.value
    user = unix.get_user(society.uid)
    res_name = yield from unix.set_real_name(user, society.description)
    yield bespoke.update_nis()
    if res_name:
        yield send(society, "tasks/society_rename.j2")
    return society
