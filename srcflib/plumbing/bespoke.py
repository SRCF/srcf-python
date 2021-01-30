"""
SRCF-specific tools.

Most methods identify users and groups using the `Member` and `Society` database models.
"""

from contextlib import contextmanager
from datetime import date
import logging
import os.path
import pwd
import shutil
import time
from typing import Generator, List, Optional, Set

from requests import Session as RequestsSession

from sqlalchemy.orm import Session as SQLASession
from sqlalchemy.orm.exc import NoResultFound

from srcf.database import Domain, HTTPSCert, Member, Session, Society
from srcf.database.queries import get_member, get_society
from srcf.database.summarise import summarise_society

from .common import command, get_members, Owner, owner_name, require_host, Result, ResultSet, State
from .mailman import MailList
from .unix import copytree_chown_chmod
from . import hosts


LOG = logging.getLogger(__name__)


@contextmanager
def context(sess: SQLASession = None) -> Generator[SQLASession, None, None]:
    """
    Run multiple database commands and commit at the end:

        >>> with context() as sess:
        ...     for ... in data:
        ...         create_member(sess, ...)
    """
    sess = sess or Session()
    try:
        yield sess
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.commit()


def get_crontab(owner: Owner) -> Optional[str]:
    """
    Fetch the owning user's crontab, if one exists on the current server.
    """
    proc = command(["/usr/bin/crontab", "-u", owner_name(owner), "-l"], output=True)
    return proc.stdout.decode("utf-8") if proc.stdout else None


def get_mailman_lists(owner: Owner, sess: RequestsSession = RequestsSession()) -> List[MailList]:
    """
    Query mailing lists owned by the given member or society.
    """
    prefix = owner_name(owner)
    resp = sess.get("https://lists.srcf.net/getlists.cgi", params={"prefix": prefix})
    return resp.text.splitlines()


def create_member(sess: SQLASession, crsid: str, preferred_name: str, surname: str, email: str,
                  mail_handler: str = "forward", is_member: bool = True,
                  is_user: bool = True) -> Result[Member]:
    """
    Register or update a member in the database.
    """
    try:
        mem = get_member(crsid, sess)
    except KeyError:
        mem = Member(crsid=crsid,
                     preferred_name=preferred_name,
                     surname=surname,
                     email=email,
                     mail_handler=mail_handler,
                     member=is_member,
                     user=is_user)
        sess.add(mem)
        state = State.success
    else:
        mem.preferred_name = preferred_name
        mem.surname = surname
        mem.email = email
        mem.mail_handler = mail_handler
        mem.member = is_member
        mem.user = is_user
        state = State.success if sess.is_modified(mem) else State.unchanged
    return Result(state, mem)


def create_society(sess: SQLASession, name: str, description: str, admins: Set[str],
                   role_email: str = None) -> Result[Society]:
    """
    Register or update a society in the database.
    """
    try:
        soc = get_society(name, sess)
    except KeyError:
        soc = Society(society=name,
                      description=description,
                      admins=get_members(sess, *admins),
                      role_email=role_email)
        sess.add(soc)
        state = State.success
    else:
        if admins != soc.admin_crsids:
            raise ValueError("Admins for {!r} are {}, expecting {}"
                             .format(name, soc.admin_crsids, admins))
        soc.description = description
        soc.role_email = role_email
        state = State.success if sess.is_modified(soc) else State.unchanged
    return Result(state, soc)


def add_to_society(sess: SQLASession, member: Member, society: Society) -> Result:
    """
    Add a new admin to a society account.
    """
    if member in society.admins:
        return Result(State.unchanged)
    society.admins.add(member)
    sess.add(society)
    return Result(State.success)


def remove_from_society(sess: SQLASession, member: Member, society: Society) -> Result:
    """
    Remove an existing admin from a society account.
    """
    if member not in society.admins:
        return Result(State.unchanged)
    society.admins.remove(member)
    sess.add(society)
    return Result(State.success)


def delete_society(sess: SQLASession, society: Society) -> Result:
    """
    Drop a society record from the database.
    """
    if society.admins:
        raise ValueError("Remove society admins for {} first".format(society))
    sess.delete(society)
    return Result(State.success)


def populate_home_dir(member: Member):
    """
    Copy the contents of ``/etc/skel`` to a new user's home directory.

    This must be done before creating anything else in the directory.
    """
    target = os.path.join("/home", member.crsid)
    if os.listdir(target):
        # Avoid potentially clobbering existing files.
        return Result(State.unchanged)
    copytree_chown_chmod("/etc/skel", os.path.join("/home", member.crsid),
                         member.uid, member.gid)
    return Result(State.success)


def link_soc_home_dir(member: Member, society: Society) -> Result:
    """
    Add or remove a user's society symlink based on their admin membership.
    """
    link = os.path.join("/home", member.crsid, society.society)
    target = os.path.join("/societies", society.society)
    try:
        current = os.readlink(link)
    except OSError:
        current = None
    valid = current == target
    needed = member in society.admins
    result = Result(State.unchanged)
    if valid == needed:
        # Includes if they're no longer an admin, and something other than the usual link exists
        # where we'd normally put this link, in which case we leave it be.
        return result
    if needed:
        try:
            os.symlink(target, link)
        except FileExistsError:
            LOG.warning("Not overwriting existing file %r", link)
        except OSError:
            LOG.warning("Couldn't symlink %r", link, exc_info=True)
        else:
            result.state = State.success
    else:
        try:
            os.unlink(link)
        except OSError:
            LOG.warning("Couldn't remove symlink %r", link, exc_info=True)
        else:
            result.state = State.success
    return result


def set_home_exim_acl(owner: Owner) -> Result:
    """
    Grant access to the user's ``.forward`` file for Exim.
    """
    path = pwd.getpwnam(owner_name(owner)).pw_dir
    command(["/usr/bin/nfs4_setfacl", "-a", "A::Debian-exim@srcf.net:RX", path])
    return Result(State.success)


def create_forwarding_file(owner: Owner) -> Result:
    """
    Write a default ``.forward`` file matching the user's external email address.
    """
    user = pwd.getpwnam(owner_name(owner))
    path = os.path.join(user.pw_dir, ".forward")
    if os.path.exists(path):
        return Result(State.unchanged)
    with open(path, "w") as f:
        f.write(owner.email + "\n")
    os.chown(path, user.pw_uid, user.pw_gid)
    return Result(State.success)


def update_quotas() -> Result:
    """
    Apply quotas from member and society limits to the filesystem.
    """
    # TODO: Port to SRCFLib, replace with entrypoint.
    command(["/usr/local/sbin/srcf-update-quotas"])
    return Result(State.success)


def set_web_status(owner: Owner, status: str) -> Result:
    """
    Add or update the owner's website type, used for Apache configuration.
    """
    username = owner_name(owner)
    key = "member" if isinstance(owner, Member) else "soc"
    path = "/societies/srcf-admin/{}webstatus".format(key)
    with open(path, "r") as f:
        data = f.read().splitlines()
    for i, line in enumerate(data):
        name, current = line.split(":", 1)
        if name != username:
            continue
        if current == status:
            return Result(State.unchanged)
        else:
            data[i] = "{}:{}".format(name, status)
            break
    else:
        data.append("{}:{}".format(name, status))
    with open(path, "w") as f:
        for line in data:
            f.write("{}\n".format(line))
    return Result(State.success)


def add_custom_domain(sess: SQLASession, owner: Owner, name: str,
                      root: str = None) -> Result[Domain]:
    """
    Assign a domain name to a member or society website.
    """
    if isinstance(owner, Member):
        class_ = "user"
    elif isinstance(owner, Society):
        class_ = "soc"
    else:
        raise TypeError(owner)
    try:
        domain = sess.query(Domain).filter(Domain.domain == name).one()
    except NoResultFound:
        domain = Domain(domain=name,
                        class_=class_,
                        owner=owner_name(owner),
                        root=root)
        sess.add(domain)
        state = State.success
    else:
        domain.class_ = class_
        domain.owner = owner_name(owner)
        domain.root = root
        state = State.success if sess.is_modified(domain) else State.unchanged
    return Result(state, domain)


def queue_https_cert(sess: SQLASession, domain: str) -> Result[HTTPSCert]:
    """
    Add an existing domain to the queue for requesting an HTTPS certificate.
    """
    assert sess.query(Domain).filter(Domain.domain == domain).count()
    try:
        cert = sess.query(HTTPSCert).filter(HTTPSCert.domain == domain).one()
    except NoResultFound:
        cert = HTTPSCert(domain=domain)
        sess.add(cert)
        state = State.success
    else:
        state = State.unchanged
    return Result(state, cert)


@require_host(hosts.WEB)
def generate_apache_groups() -> Result:
    """
    Synchronise the Apache groups file, providing ``srcfmembers`` and ``srcfusers`` groups.
    """
    # TODO: Port to SRCFLib, replace with entrypoint.
    command(["/usr/local/sbin/srcf-updateapachegroups"])
    return Result(State.success)


def queue_list_subscription(member: Member, *lists: str) -> Result:
    """
    Subscribe the user to one or more mailing lists.
    """
    if not lists:
        return Result(State.unchanged)
    # TODO: Port to SRCFLib, replace with entrypoint.
    entry = '"{}" <{}>'.format(member.name, member.email)
    args = ["/usr/local/sbin/srcf-enqueue-mlsub"]
    for name in lists:
        args.append("soc-srcf-{}:{}".format(name, entry))
    command(args)
    return Result(State.success)


def generate_sudoers() -> Result:
    """
    Update sudo permissions to allow admins to exdcute commands under their society accounts.
    """
    # TODO: Port to SRCFLib, replace with entrypoint.
    command(["/usr/local/sbin/srcf-generate-society-sudoers"])
    return Result(State.success)


def export_members() -> Result:
    """
    Regenerate the legacy membership lists.
    """
    # TODO: Port to SRCFLib, replace with entrypoint.
    command(["/usr/local/sbin/srcf-memberdb-export"])
    return Result(State.success)


@require_host(hosts.USER)
def update_nis(wait: bool = False) -> Result:
    """
    Synchronise UNIX users and passwords over NIS.

    If a new user or group has just been created, and is about to be used, set ``wait`` to avoid
    the caching of non-existent UIDs or GIDs.
    """
    command(["/usr/bin/make", "-C", "/var/yp"])
    if wait:
        time.sleep(16)
    return Result(State.success)


@require_host(hosts.LIST)
def configure_mailing_list(name: str) -> Result:
    """
    Apply default options to a new mailing list, and create the necessary mail aliases.
    """
    command(["/usr/sbin/config_list", "--inputfile", "/root/mailman-newlist-defaults", name])
    return Result(State.success)


@require_host(hosts.LIST)
def generate_mailman_aliases() -> Result:
    """
    Refresh the Exim alias file for Mailman lists.
    """
    # TODO: Port to SRCFLib, replace with entrypoint.
    command(["/usr/local/sbin/srcf-generate-mailman-aliases"])
    return Result(State.success)


def archive_society_files(society: Society) -> Result[str]:
    """
    Create a backup of the society under /archive/societies.
    """
    home = os.path.join("/societies", society.society)
    public = os.path.join("/public/societies", society.society)
    root = os.path.join("/archive/societies", society.society)
    os.mkdir(root)
    tar = os.path.join(root, "soc-{}-{}.tar.bz2".format(society.society,
                                                        date.today().strftime("%Y%m%d")))
    command(["/bin/tar", "cjf", tar, home, public])
    crontab = get_crontab(society)
    if crontab:
        with open(os.path.join(root, "crontab"), "w") as f:
            f.write(crontab)
    # TOOD: for host in {"cavein", "sinkhole"}: get_crontab(society)
    with open(os.path.join(root, "society_info"), "w") as f:
        f.write(summarise_society(society))
    return Result(State.success, tar)


def delete_society_files(society: Society) -> ResultSet:
    """
    Remove all public and private files of a society in /home.
    """
    home = os.path.join("/societies", society.society)
    public = os.path.join("/public/societies", society.society)
    results = ResultSet()
    for path in (home, public):
        if os.path.exists(path):
            shutil.rmtree(home)
            results.extend(Result(State.success))
        else:
            results.extend(Result(State.unchanged))
    return results


def slay_user(owner: Owner) -> Result:
    """
    Kill all processes belonging to the given account.
    """
    proc = command(["/usr/local/sbin/srcf-slay", owner_name(owner)], output=True)
    return Result(State.success if proc.stdout else State.unchanged)
