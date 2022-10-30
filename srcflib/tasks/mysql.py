"""
MySQL accounts and databases for members and societies.
"""

from typing import List, Optional, Set, Tuple, Union

from pymysql.cursors import Cursor

from srcf.database import Member, Society
from srcf.database.queries import get_member, get_society

from ..email import send
from ..plumbing import mysql
from ..plumbing.common import Collect, Owner, owner_name, Password, Result, State


# Re-export connection plumbing to avoid unnecessary imports elsewhere.
connect = mysql.connect
context = mysql.context


def _user_name(owner: Owner) -> str:
    return owner_name(owner).replace("-", "_")


def _user_name_rev(name: str) -> str:
    return name.replace("_", "-")


def _database_name(name: Union[str, Owner], suffix: Optional[str] = None) -> str:
    if not isinstance(name, str):
        name = _user_name(name)
    if suffix:
        name = "{}/{}".format(name, suffix)
    return name


def _database_name_rev(name: str) -> str:
    return _user_name_rev(name.split("/", 1)[0])


def get_owned_databases(cursor: Cursor, owner: Owner) -> List[str]:
    """
    Find all MySQL databases belonging to a given owner.
    """
    return (mysql.get_matched_databases(cursor, _database_name(owner))
            + mysql.get_matched_databases(cursor, _database_name(owner, "%")))


@Result.collect_value
def new_account(cursor: Cursor, owner: Owner) -> Collect[Optional[Password]]:
    """
    Create a MySQL user account for a given member or society.

    For members, grants are added to all society databases for which they are a member.
    """
    user = _user_name(owner)
    res_passwd = yield from mysql.ensure_user(cursor, user)
    yield mysql.grant_database(cursor, user, _database_name(owner))
    yield mysql.grant_database(cursor, user, _database_name(owner, "%"))
    if isinstance(owner, Member):
        yield sync_member_roles(cursor, owner)
    elif isinstance(owner, Society):
        yield sync_society_roles(cursor, owner)
    return res_passwd.value


def _sync_roles(cursor: Cursor, current: Set[Tuple[str, str]],
                needed: Set[Tuple[str, str]]):
    for user, database in needed - current:
        yield mysql.grant_database(cursor, user, database)
    for user, database in current - needed:
        yield mysql.revoke_database(cursor, user, database)


@Result.collect
def sync_member_roles(cursor: Cursor, member: Member) -> Collect[None]:
    """
    Adjust grants for society roles to match the given member's memberships.
    """
    user = _user_name(member)
    current: Set[Tuple[str, str]] = set()
    seen: Set[str] = set()
    for database in mysql.get_user_databases(cursor, user):
        name = _database_name_rev(database)
        # Filter active roles to those owned by society accounts.
        if name == member.crsid:
            continue
        if name not in seen:
            try:
                get_society(name)
            except KeyError:
                continue
            else:
                seen.add(name)
        if name in seen:
            current.add((user, database))
    needed: Set[Tuple[str, str]] = set()
    if member.societies:
        for role in mysql.get_users(cursor, *(_user_name(soc) for soc in member.societies)):
            databases = (_database_name(role), _database_name(role, "%"))
            needed.update({(user, database) for database in databases})
        yield from _sync_roles(cursor, current, needed)


@Result.collect
def sync_society_roles(cursor: Cursor, society: Society) -> Collect[None]:
    """
    Adjust grants for member roles to match the given society's admins.
    """
    databases = (_database_name(society), _database_name(society, "%"))
    current: Set[Tuple[str, str]] = set()
    for database in databases:
        for user in mysql.get_database_users(cursor, database):
            # Filter active roles to those owned by society accounts.
            username = _user_name_rev(user)
            if username == society.society:
                continue
            try:
                get_member(username)
            except KeyError:
                continue
            else:
                current.add((user, database))
    needed: Set[Tuple[str, str]] = set()
    for user in mysql.get_users(cursor, *society.admin_crsids):
        needed.update({(user, database) for database in databases})
    yield from _sync_roles(cursor, current, needed)


@Result.collect_value
def reset_password(cursor: Cursor, owner: Owner) -> Collect[Password]:
    """
    Reset the password of a member's or society's MySQL user account.
    """
    res_passwd = yield from mysql.reset_password(cursor, _user_name(owner))
    yield send(owner, "tasks/mysql_password.j2", {"username": _user_name(owner),
                                                  "password": res_passwd.value})
    return res_passwd.value


@Result.collect
def drop_account(cursor: Cursor, owner: Owner) -> Collect[None]:
    """
    Drop a MySQL user account for a given member or society.

    For members, grants are removed from all society databases for which they are a member.
    """
    user = _user_name(owner)
    yield mysql.revoke_database(cursor, user, _database_name(owner))
    yield mysql.revoke_database(cursor, user, _database_name(owner, "%"))
    if isinstance(owner, Member):
        for soc in owner.societies:
            yield mysql.revoke_database(cursor, user, _database_name(soc))
            yield mysql.revoke_database(cursor, user, _database_name(soc, "%"))
    yield mysql.drop_user(cursor, user)


@Result.collect_value
def create_database(cursor: Cursor, owner: Owner, suffix: Optional[str] = None) -> Collect[str]:
    """
    Create a new MySQL database for the owner, either the primary name or a suffixed alternative.
    """
    name = _database_name(owner, suffix)
    yield mysql.create_database(cursor, name)
    return name


@Result.collect_value
def drop_database(cursor: Cursor, owner: Owner, suffix: Optional[str] = None) -> Collect[str]:
    """
    Drop either the primary or a suffixed secondary MySQL database belonging to the owner.
    """
    name = _database_name(owner, suffix)
    yield mysql.drop_database(cursor, name)
    return name


@Result.collect
def drop_all_databases(cursor: Cursor, owner: Owner) -> Collect[None]:
    """
    Drop all databases belonging to the owner.
    """
    for database in get_owned_databases(cursor, owner):
        yield mysql.drop_database(cursor, database)


@Result.collect_value
def create_account(cursor: Cursor, owner: Owner) -> Collect[Tuple[Optional[Password], str]]:
    """
    Create a MySQL user account and initial database for a member or society.
    """
    res_account = yield from new_account(cursor, owner)
    res_db = yield from create_database(cursor, owner)
    if res_account.state == State.created:
        yield send(owner, "tasks/mysql_create.j2", {"username": _user_name(owner),
                                                    "password": res_account.value,
                                                    "database": res_db.value})
    return (res_account.value, res_db.value)
