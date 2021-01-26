"""
Unix user management.

Most methods identify users and groups using the `pwd` and `grp` module structs.
"""

import grp
import logging
import os
import pwd

# Expose these here for now, so that other parts of SRCFLib can reference them locally, but keep a
# single implementation in case it needs revising.  TODO: Move here as part of control migration.
from srcf.controllib.utils import copytree_chown_chmod, nfs_aware_chown

from .common import command, Password, require_host, Result, ResultSet, State
from . import hosts


# Type aliases for external callers, who need not be aware of the internal structure when chaining
# calls (e.g. get_user/create_user -> reset_password).
User = pwd.struct_passwd
Group = grp.struct_group


LOG = logging.getLogger(__name__)

_NOLOGIN_SHELLS = ("/bin/false", "/usr/sbin/nologin")


def get_user(username: str) -> User:
    """
    Look up an existing user by name.
    """
    return pwd.getpwnam(username)


def get_group(username: str) -> Group:
    """
    Look up an existing group by name.
    """
    return grp.getgrnam(username)


@require_host(hosts.USER)
def add_user(username: str, uid: int = None, system: bool = False, active: bool = True,
             home_dir: str = None, real_name: str = "") -> Result[User]:
    """
    Create a new user account.  System users are created with an empty home directory, whereas
    regular users inherit from ``/etc/skel``.
    """
    try:
        get_user(username)
    except KeyError:
        pass
    else:
        raise ValueError("Username {!r} is already in use".format(username))
    args = ["/usr/sbin/adduser", "--disabled-password", "--no-create-home", username]
    if uid:
        try:
            user = pwd.getpwuid(uid)
        except KeyError:
            # Don't set --gid, this implies an existing group -- uid == gid by default.
            args[-1:-1] = ["--uid", str(uid)]
        else:
            raise ValueError("UID {} is already in use by {!r}".format(uid, user.pw_name))
    if system:
        # Don't auto-create home directory as it will clone from /etc/skel.
        args[-1:-1] = ["--system", "--no-create-home"]
    if not active:
        args[-1:-1] = ["--shell", _NOLOGIN_SHELLS[0]]
    if home_dir:
        args[-1:-1] = ["--home", home_dir]
    if real_name:
        args[-1:-1] = ["--gecos", real_name]
    command(args)
    user = get_user(username)
    if system and home_dir:
        create_home(user, home_dir)
    return Result(State.success, user)


@require_host(hosts.USER)
def enable_user(user: User, active: bool = True) -> Result:
    """
    Change the default shell for this user, using a no-login shell to disable, and bash to enable.
    """
    login = user.pw_shell not in _NOLOGIN_SHELLS
    if login and not active:
        command(["/usr/bin/chsh", "--shell", "/bin/bash", user.pw_name])
        return Result(State.success)
    elif active and not login:
        command(["/usr/bin/chsh", "--shell", _NOLOGIN_SHELLS[0], user.pw_name])
        return Result(State.success)
    else:
        return Result(State.unchanged)


@require_host(hosts.USER)
def set_real_name(user: User, real_name: str = "") -> Result:
    """
    Update a user's GECOS name field.
    """
    current, *rest = user.pw_gecos.split(",")
    if current == real_name:
        return Result(State.unchanged)
    command(["/usr/bin/chfn", "--full-name", real_name, user.pw_name])
    return Result(State.success)


@require_host(hosts.USER)
def reset_password(user: User) -> Result[Password]:
    """
    Set the user's password to a new random value.
    """
    passwd = Password.new()
    command(["/usr/sbin/chpasswd"], passwd.wrap("{}:{{}}".format(user.pw_name)))
    return Result(State.success, passwd)


def create_home(user: User, path: str, world_read: bool = False) -> Result:
    """
    Create an empty home directory owned by the given user.
    """
    result = Result(State.unchanged)
    try:
        os.mkdir(path, 0o2775 if world_read else 0o2770)
    except FileExistsError:
        pass
    else:
        result.state = State.success
    stat = os.stat(path)
    if stat.st_uid != user.pw_uid or stat.st_gid != user.pw_gid:
        nfs_aware_chown(path, user.pw_uid, user.pw_gid)
        result.state = State.success
    return result


def create_user(username: str, uid: int = None, system: bool = False, active: bool = True,
                home_dir: str = None, real_name: str = "") -> Result[User]:
    """
    Create a new user account, or enable/disable an existing one.
    """
    try:
        user = get_user(username)
    except KeyError:
        return add_user(username, uid, system, active, home_dir, real_name)
    else:
        if user.pw_uid != uid:
            raise ValueError("User {!r} has UID {}, expected {}".format(username, user.pw_uid, uid))
        if user.pw_dir != home_dir:
            raise ValueError("User {!r} has home directory {!r}, expected {!r}"
                             .format(username, user.pw_dir, home_dir))
        result = ResultSet[User](set_real_name(user, real_name),
                                 enable_user(user, active))
        result.value = user
        return result


@require_host(hosts.USER)
def add_group(username: str, gid: int = None, system: bool = False) -> Result[Group]:
    """
    Create a new group.
    """
    try:
        get_group(username)
    except KeyError:
        pass
    else:
        raise ValueError("Username {!r} is already in use".format(username))
    args = ["/usr/sbin/addgroup", username]
    if gid:
        try:
            group = grp.getgrgid(gid)
        except KeyError:
            args[-1:-1] = ["--gid", str(gid)]
        else:
            raise ValueError("GID {} is already in use by {!r}".format(gid, group.gr_name))
    if system:
        args[-1:-1] = ["--system"]
    command(args)
    return Result(State.success, get_group(username))


@require_host(hosts.USER)
def add_to_group(user: User, group: Group) -> Result:
    """
    Add a user to a secondary group.
    """
    if user.pw_name in group.gr_mem:
        return Result(State.unchanged)
    command(["/usr/sbin/addgroup", user.pw_name, group.gr_name])
    group.gr_mem.append(user.pw_name)
    return Result(State.success)


@require_host(hosts.USER)
def remove_from_group(user: User, group: Group) -> Result:
    """
    Remove a user from a secondary group.
    """
    if user.pw_name not in group.gr_mem:
        return Result(State.unchanged)
    command(["/usr/sbin/deluser", user.pw_name, group.gr_name])
    group.gr_mem.remove(user.pw_name)
    return Result(State.success)


def create_group(username: str, gid: int = None, system: bool = False) -> Result[Group]:
    """
    Create a new or retrieve an existing group.
    """
    try:
        group = get_group(username)
    except KeyError:
        return add_group(username, gid, system)
    else:
        if group.gr_gid != gid:
            raise ValueError("Group {!r} has GID {}, expected {}"
                             .format(username, group.gr_gid, gid))
        return Result(State.unchanged, group)
