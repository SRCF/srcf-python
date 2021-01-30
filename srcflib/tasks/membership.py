"""
Creation and management of member and society accounts.
"""

import os
from typing import Set, Tuple

from srcf.database import Member, Society
from srcf.database.queries import get_member, get_society

from ..plumbing import bespoke, mailman, Password, pgsql as pgsql_p, ResultSet, unix
from ..plumbing.mysql import context as mysql_context
from . import mysql, pgsql


def create_member(crsid: str, preferred_name: str, surname: str, email: str,
                  mail_handler: str, is_member: bool = True, is_user: bool = True,
                  social: bool = False) -> ResultSet[Tuple[Member, Password]]:
    """
    Register and provision a new member of the SRCF.
    """
    results = ResultSet[Tuple[Member, Password]]()
    with bespoke.context() as sess:
        result = bespoke.create_member(sess, crsid, preferred_name, surname, email,
                                       mail_handler, is_member, is_user)
        mem = results.add(result).value
    user = results.add(unix.create_user(crsid, uid=mem.uid, system=True,
                                        home_dir=os.path.join("/home", crsid),
                                        real_name=mem.name)).value
    passwd = results.add(unix.reset_password(user)).value
    results.extend(bespoke.update_nis(True),
                   unix.create_home(user, os.path.join("/public/home", crsid), True),
                   bespoke.set_home_exim_acl(mem),
                   bespoke.populate_home_dir(mem),
                   bespoke.update_quotas())
    if mail_handler == "pip":
        results.extend(bespoke.create_forwarding_file(mem))
    # TODO: Legacy mailbox creation
    results.extend(bespoke.set_web_status(mem, "subdomain"),
                   bespoke.queue_list_subscription(mem, "maintenance"))
    if social:
        results.extend(bespoke.queue_list_subscription(mem, "social"))
    results.extend(bespoke.generate_apache_groups(),
                   bespoke.export_members())
    # TODO: Welcome email
    results.value = (mem, passwd)
    return results


def create_sysadmin(member: Member) -> ResultSet:
    """
    Create an administrative account for an existing member.
    """
    if not member.user:
        raise ValueError("{!r} is not an active user")
    username = "{}-adm".format(member.crsid)
    real_name = "{} (Sysadmin Account)".format(member.name)
    results = ResultSet()
    user = results.add(unix.create_user(username, real_name=real_name)).value
    results.extend(unix.add_to_group(user, unix.get_group("sysadmins")),
                   unix.add_to_group(user, unix.get_group("adm")))
    # TODO: sed -i~ -re "/^sysadmin/ s/$/ (,$admuser,)/" /etc/netgroup
    for soc in ("executive", "srcf-admin", "srcf-web"):
        results.extend(add_society_admin(member, get_society(soc)))
    with pgsql_p.context() as cursor:
        results.extend(pgsql_p.create_user(cursor, username),
                       pgsql_p.grant_role(cursor, username, pgsql_p.get_role(cursor, "sysadmins")))
    return results


def update_member_name(member: Member, preferred_name: str, surname: str) -> ResultSet:
    """
    Update a member's registered name.
    """
    results = ResultSet()
    with bespoke.context() as sess:
        member = results.add(bespoke.create_member(sess=sess, crsid=member.crsid,
                                                   preferred_name=preferred_name,
                                                   surname=surname,
                                                   email=member.email,
                                                   mail_handler=member.mail_handler,
                                                   is_member=member.member,
                                                   is_user=member.user)).value
    pwd_info = unix.get_user(member.crsid)
    results.extend(unix.set_real_name(pwd_info, member.name),
                   bespoke.update_nis())
    return results


def create_society(name: str, description: str, admins: Set[str],
                   role_email: str = None) -> ResultSet[Society]:
    """
    Register a new SRCF society account.
    """
    results = ResultSet[Society]()
    with bespoke.context() as sess:
        soc = results.add(bespoke.create_society(sess, name, description, admins,
                                                 role_email), True).value
    user = results.add(unix.create_user(name, uid=soc.uid, system=True, active=False,
                                        home_dir=os.path.join("/societies", name),
                                        real_name=description)).value
    group = results.add(unix.create_group(name, gid=soc.gid, system=True)).value
    results.extend(bespoke.update_nis(True),
                   unix.create_home(user, os.path.join("/public/societies", name), True),
                   bespoke.set_home_exim_acl(soc),
                   bespoke.update_quotas())
    for admin in admins:
        results.extend(unix.add_to_group(unix.get_user(admin), group),
                       bespoke.link_soc_home_dir(get_member(admin), soc))
    results.extend(bespoke.set_web_status(soc, "subdomain"),
                   bespoke.generate_apache_groups(),
                   bespoke.generate_sudoers(),
                   bespoke.export_members())
    # TODO: Welcome email
    # TODO: Existing admins email
    return results


def add_society_admin(member: Member, society: Society) -> ResultSet:
    """
    Promote a member to a society account admin.
    """
    with bespoke.context() as sess:
        # Re-fetch under current session for transaction safety.
        member = get_member(member.crsid, sess)
        society = get_society(society.society, sess)
        results = ResultSet(bespoke.add_to_society(sess, member, society))
    results.extend(unix.add_to_group(unix.get_user(member.crsid), unix.get_group(society.society)),
                   bespoke.link_soc_home_dir(member, society))
    with mysql_context() as cursor:
        results.extend(mysql.sync_society_roles(cursor, member))
    with pgsql_p.context() as cursor:
        results.extend(pgsql.sync_society_roles(cursor, member))
    return results


def remove_society_admin(member: Member, society: Society) -> ResultSet:
    """
    Demote a member from a society account's list of admins.
    """
    with bespoke.context() as sess:
        member = get_member(member.crsid, sess)
        society = get_society(society.society, sess)
        results = ResultSet(bespoke.remove_from_society(sess, member, society))
    results.extend(unix.remove_from_group(unix.get_user(member.crsid),
                                          unix.get_group(society.society)),
                   bespoke.link_soc_home_dir(member, society))
    with mysql_context() as cursor:
        results.extend(mysql.sync_society_roles(cursor, member))
    with pgsql_p.context() as cursor:
        results.extend(pgsql.sync_society_roles(cursor, member))
    return results


def delete_society(society: Society) -> ResultSet:
    """
    Archive and delete all traces of a society account.
    """
    results = ResultSet()
    for mem in society.admins:
        results.extend(remove_society_admin(mem, society))
    results.extend(bespoke.slay_user(society),
                   # TODO: for server in {"cavein", "sinkhole"}: bespoke.slay_user(society)
                   bespoke.archive_society_files(society),
                   bespoke.delete_society_files(society))
    with mysql_context() as cursor:
        mysql.drop_all_databases(cursor, society)
        mysql.drop_account(cursor, society)
    with pgsql_p.context() as cursor:
        pgsql.drop_all_databases(cursor, society)
        pgsql.drop_account(cursor, society)
    for mlist in bespoke.get_mailman_lists(society):
        mailman.remove_list(mlist)
    # TODO: Unix user/group
    with bespoke.context() as sess:
        results.extend(bespoke.delete_society(sess, society))
    results.extend(bespoke.export_members())
    return results


def update_society_description(society: Society, description: str) -> ResultSet:
    """
    Update a society's description ('full name').
    """
    results = ResultSet()
    with bespoke.context() as sess:
        society = results.add(bespoke.create_society(sess=sess, name=society.society,
                                                     description=description,
                                                     admins=society.admin_crsids,
                                                     role_email=society.role_email)).value
    pwd_info = unix.get_user(society.society)
    results.extend(unix.set_real_name(pwd_info, description),
                   bespoke.update_nis())
    return results
