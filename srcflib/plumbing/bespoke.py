"""
SRCF-specific tools.

Most methods identify users and groups using the `Member` and `Society` database models.
"""

from datetime import date, datetime
import logging
import os
import pwd
import shutil
from subprocess import CalledProcessError
import time
from typing import List, Optional

from requests import Session as RequestsSession

from sqlalchemy.orm import Session as SQLASession
from sqlalchemy.orm.exc import NoResultFound

from srcf.controllib import jobs
from srcf.database import Domain, HTTPSCert, Job, MailHandler, Member, Society
from srcf.database.queries import get_member, get_society
from srcf.database.summarise import summarise_society

from .common import (Collect, command, make, Owner, owner_home, owner_name, require_host, Result,
                     State, Unset)
from .mailman import MailList
from . import hosts, unix
from ..email import send


LOG = logging.getLogger(__name__)


def log_to_file(path: str, message: str) -> Result[Unset]:
    """
    Write a timestamped line to a log file.
    """
    with open(path, "a") as log:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.write("{} -- {}\n".format(now, message))
    return Result(State.success)


def get_crontab(owner: Owner) -> Optional[str]:
    """
    Fetch the owning user's crontab, if one exists on the current server.
    """
    try:
        proc = command(["/usr/bin/crontab", "-u", owner_name(owner), "-l"], output=True)
    except CalledProcessError:
        return None
    if proc.stdout:
        return proc.stdout.decode("utf-8")
    else:
        return None


def clear_crontab(owner: Owner) -> Result[Unset]:
    """
    Clear the owning user's crontab, if one exists on the current server.
    """
    if not get_crontab(owner):
        return Result(State.unchanged)
    command(["/usr/bin/crontab", "-u", owner_name(owner), "-r"])
    return Result(State.success)


def get_mailman_lists(owner: Owner, sess: RequestsSession = RequestsSession()) -> List[MailList]:
    """
    Query mailing lists owned by the given member or society.
    """
    prefix = owner_name(owner)
    resp = sess.get("https://lists.srcf.net/getlists.cgi", params={"prefix": prefix})
    return [MailList(name) for name in resp.text.splitlines()]


def _create_member(sess: SQLASession, crsid: str, preferred_name: Optional[str],
                   surname: Optional[str], email: Optional[str],
                   mail_handler: MailHandler = MailHandler.forward, is_member: bool = True,
                   is_user: bool = True, is_contactable: bool = True) -> Result[Member]:
    member = Member(crsid=crsid,
                    preferred_name=preferred_name,
                    surname=surname,
                    email=email,
                    mail_handler=mail_handler.name,
                    member=is_member,
                    user=is_user,
                    contactable=is_contactable)
    sess.add(member)
    # Populate UID and GID from the database.
    sess.flush()
    LOG.debug("Created member record: %r", member)
    return Result(State.created, member)


def _update_member(sess: SQLASession, member: Member, preferred_name: Optional[str],
                   surname: Optional[str], email: Optional[str],
                   mail_handler: MailHandler = MailHandler.forward,
                   is_member: bool = True, is_user: bool = True,
                   is_contactable: bool = True) -> Result[Unset]:
    member.preferred_name = preferred_name
    member.surname = surname
    member.email = email
    member.mail_handler = mail_handler.name
    member.member = is_member
    member.user = is_user
    member.contactable = is_contactable
    if not sess.is_modified(member):
        return Result(State.unchanged)
    LOG.debug("Updated member record: %r", member)
    return Result(State.success)


@Result.collect_value
def ensure_member(sess: SQLASession, crsid: str, preferred_name: Optional[str],
                  surname: Optional[str], email: Optional[str],
                  mail_handler: MailHandler = MailHandler.forward, is_member: bool = True,
                  is_user: bool = True, is_contactable: bool = True) -> Collect[Member]:
    """
    Register or update a member in the database.
    """
    try:
        member = get_member(crsid, sess, include_non_members=True)
    except KeyError:
        res_record = yield from _create_member(sess, crsid, preferred_name, surname, email,
                                               mail_handler, is_member, is_user, is_contactable)
        member = res_record.value
    else:
        yield _update_member(sess, member, preferred_name, surname, email, mail_handler,
                             is_member, is_user, is_contactable)
    return member


def _create_society(sess: SQLASession, name: str, description: str,
                    role_email: Optional[str] = None) -> Result[Society]:
    society = Society(society=name,
                      description=description,
                      role_email=role_email)
    sess.add(society)
    # Populate UID and GID from the database.
    sess.flush()
    LOG.debug("Created society record: %r", society)
    return Result(State.created, society)


def _update_society(sess: SQLASession, society: Society, description: str,
                    role_email: Optional[str]) -> Result[Unset]:
    society.description = description
    society.role_email = role_email
    if not sess.is_modified(society):
        return Result(State.unchanged)
    LOG.debug("Updated society record: %r", society)
    return Result(State.success)


def delete_society(sess: SQLASession, society: Society) -> Result[Unset]:
    """
    Drop a society record from the database.
    """
    if society.admins:
        raise ValueError("Remove society admins for {} first".format(society))
    if get_custom_domains(sess, society):
        raise ValueError("Remove domains for {} first".format(society))
    sess.delete(society)
    LOG.debug("Deleted society record: %r", society)
    return Result(State.success)


@Result.collect_value
def ensure_society(sess: SQLASession, name: str, description: str,
                   role_email: Optional[str] = None) -> Collect[Society]:
    """
    Register or update a society in the database.

    For existing societies, this will synchronise member relations with the given list of admins.
    """
    try:
        society = get_society(name, sess)
    except KeyError:
        res_record = yield from _create_society(sess, name, description, role_email)
        society = res_record.value
    else:
        yield _update_society(sess, society, description, role_email)
    return society


def _add_to_society(sess: SQLASession, member: Member, society: Society) -> Result[Unset]:
    if member in society.admins:
        return Result(State.unchanged)
    society.admins.add(member)
    LOG.debug("Added society admin: %r %r", member, society)
    return Result(State.success)


def _remove_from_society(sess: SQLASession, member: Member, society: Society) -> Result[Unset]:
    if member not in society.admins:
        return Result(State.unchanged)
    society.admins.remove(member)
    LOG.debug("Removed society admin: %r %r", member, society)
    return Result(State.success)


@Result.collect
def add_society_admin(sess: SQLASession, member: Member, society: Society,
                      group: unix.Group) -> Collect[None]:
    """
    Add a new admin to a society account.
    """
    yield _add_to_society(sess, member, society)
    yield unix.add_to_group(unix.get_user(member.uid), group)
    yield link_soc_home_dir(member, society)


@Result.collect
def remove_society_admin(sess: SQLASession, member: Member, society: Society,
                         group: unix.Group) -> Collect[None]:
    """
    Remove an existing admin from a society account.
    """
    yield _remove_from_society(sess, member, society)
    yield unix.remove_from_group(unix.get_user(member.uid), group)
    yield link_soc_home_dir(member, society)


def populate_home_dir(member: Member) -> Result[Unset]:
    """
    Copy the contents of ``/etc/skel`` to a new user's home directory.

    This must be done before creating anything else in the directory.
    """
    target = owner_home(member)
    if os.listdir(target):
        # Avoid potentially clobbering existing files.
        return Result(State.unchanged)
    unix.copytree_chown_chmod("/etc/skel", target, member.uid, member.gid)
    return Result(State.success)


@Result.collect
def create_public_html(owner: Owner) -> Collect[None]:
    """
    Create a user's public_html directory inside their public directory.

    For creations before April 2023, also add a symlink to it in their home directory.
    """
    user = unix.get_user(owner.uid)
    public_html = os.path.join(owner_home(owner, True), "public_html")
    yield unix.mkdir(public_html, user)
    symlink_cutoff = date(2023, 4, 1)  # accounts provisioned from this date don't get the symlink
    if date.today() < symlink_cutoff:
        link = os.path.join(owner_home(owner), "public_html")
        yield unix.symlink(link, public_html)


@Result.collect
def link_soc_home_dir(member: Member, society: Society) -> Collect[None]:
    """
    Add or remove a user's society symlink based on their admin membership.
    """
    link = os.path.join(owner_home(member), society.society)
    target = owner_home(society)
    yield unix.symlink(link, target, member in society.admins)


@Result.collect
def set_home_exim_acl(owner: Owner) -> Collect[None]:
    """
    Grant access to the user's ``.forward`` file for Exim.
    """
    yield unix.set_nfs_acl(owner_home(owner), "Debian-exim@srcf.net", "RX")


def create_forwarding_file(owner: Owner) -> Result[Unset]:
    """
    Write a default ``.forward`` file matching the user's external email address.
    """
    path = os.path.join(owner_home(owner), ".forward")
    if os.path.exists(path):
        return Result(State.unchanged)
    with open(path, "w") as f:
        f.write("{}\n".format(owner.email))
    user = pwd.getpwnam(owner_name(owner))
    os.chown(path, user.pw_uid, user.pw_gid)
    LOG.debug("Created forwarding file: %r", path)
    return Result(State.created)


def create_legacy_mailbox(member: Member) -> Result[Unset]:
    """
    Send an email to a user's legacy mailbox.
    """
    if os.path.exists(os.path.join("/var/mail", member.crsid)):
        return Result(State.unchanged)
    res_send = send((member.name, "real-{}@srcf.net".format(member.crsid)),
                    "plumbing/legacy_mailbox.j2", {"target": member})
    return Result(State.created, parts=(res_send,))


def empty_legacy_mailbox(member: Member) -> Result[Unset]:
    """
    Delete all messages inside a user's legacy mailbox.
    """
    path = os.path.join("/var/mail", member.crsid)
    try:
        stats = os.stat(path)
    except FileNotFoundError:
        return Result(State.unchanged)
    if stats.st_size == 0:
        return Result(State.unchanged)
    os.truncate(path, 0)
    return Result(State.success)


@Result.collect
def scrub_user(owner: Owner) -> Collect[None]:
    """
    Anonymise the Unix user of a member or society.
    """
    try:
        user = unix.get_user(owner.uid)
    except KeyError:
        return
    else:
        cls = "soc" if isinstance(owner, Society) else "user"
        yield unix.set_real_name(user, "")
        if isinstance(owner, Society):
            yield unix.set_home_dir(user, "/nonexistent")
        yield unix.rename_user(user, "ex{}{}".format(cls, owner.uid))


def scrub_group(owner: Owner) -> Result[Unset]:
    """
    Anonymise the Unix group of a member or society.
    """
    try:
        group = unix.get_group(owner.gid)
    except KeyError:
        return Result(State.unchanged)
    else:
        cls = "soc" if isinstance(owner, Society) else "user"
        return unix.rename_group(group, "ex{}{}".format(cls, owner.gid))


def scrub_member_jobs(sess: SQLASession, owner: Owner) -> Result[Unset]:
    """
    Erase sensitive fields of all jobs submitted to the Control Panel by this member or society.
    """
    state = State.unchanged
    query = sess.query(Job)
    if isinstance(owner, Member):
        query = query.filter((Job.owner_crsid == owner.crsid) |
                             ((Job.type == jobs.Signup.JOB_TYPE) &
                              (Job.args.contains({"crsid": owner.crsid}))))
    elif isinstance(owner, Society):
        query = query.filter(Job.args.contains({"society": owner.society}))
    else:
        raise TypeError(owner)
    for job in query:
        cls = jobs.all_jobs[job.type]
        if cls not in jobs.SENSITIVE_ARGS:
            continue
        for field in jobs.SENSITIVE_ARGS[cls]:
            value = job.args.get(field)
            if value and value != "<redacted>":
                LOG.debug("Scrubbing job #%d (%s), field %r", job.job_id, job.type, field)
                job.args[field] = "<redacted>"
                state = State.success
    return Result(state)


def update_quotas() -> Result[Unset]:
    """
    Apply quotas from member and society limits to the filesystem.
    """
    # TODO: Port to SRCFLib, replace with entrypoint.
    command(["/usr/local/sbin/srcf-update-quotas"])
    return Result(State.success)


def get_custom_domains(sess: SQLASession, owner: Owner) -> List[Domain]:
    """
    Retrieve all custom domains assigned to a member or society.
    """
    if isinstance(owner, Member):
        class_ = "user"
    elif isinstance(owner, Society):
        class_ = "soc"
    else:
        raise TypeError(owner)
    return list(sess.query(Domain).filter(Domain.class_ == class_,
                                          Domain.owner == owner_name(owner)))


def add_custom_domain(sess: SQLASession, owner: Owner, name: str,
                      root: Optional[str] = None) -> Result[Domain]:
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
        state = State.created
        LOG.debug("Created domain record: %r", domain)
    else:
        domain.class_ = class_
        domain.owner = owner_name(owner)
        domain.root = root
        if sess.is_modified(domain):
            state = State.success
            LOG.debug("Updated domain record: %r", domain)
        else:
            state = State.unchanged
    return Result(state, domain)


def remove_custom_domain(sess: SQLASession, owner: Owner, name: str) -> Result[Unset]:
    """
    Unassign a domain name from a member or society.
    """
    try:
        domain = sess.query(Domain).filter(Domain.domain == name).one()
    except NoResultFound:
        state = State.unchanged
    else:
        sess.delete(domain)
        state = State.success
        LOG.debug("Deleted domain record: %r", domain)
    return Result(state)


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
        state = State.created
        LOG.debug("Created HTTPS cert record: %r", cert)
    else:
        state = State.unchanged
    return Result(state, cert)


@require_host(hosts.WEB)
def generate_apache_groups() -> Result[Unset]:
    """
    Synchronise the Apache groups file, providing ``srcfmembers`` and ``srcfusers`` groups.
    """
    # TODO: Port to SRCFLib, replace with entrypoint.
    command(["/usr/local/sbin/srcf-updateapachegroups"])
    return Result(State.success)


def queue_list_subscription(member: Member, *lists: str) -> Result[Unset]:
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
    LOG.debug("Queued list subscriptions: %r %r", member, lists)
    return Result(State.success)


def generate_sudoers() -> Result[Unset]:
    """
    Update sudo permissions to allow admins to exdcute commands under their society accounts.
    """
    # TODO: Port to SRCFLib, replace with entrypoint.
    command(["/usr/local/sbin/srcf-generate-society-sudoers"])
    return Result(State.success)


def export_members() -> Result[Unset]:
    """
    Regenerate the legacy membership lists.
    """
    # TODO: Port to SRCFLib, replace with entrypoint.
    command(["/usr/local/sbin/srcf-memberdb-export"])
    return Result(State.success)


@require_host(hosts.USER)
@Result.collect
def update_nis(wait: bool = False) -> Collect[None]:
    """
    Synchronise UNIX users and passwords over NIS.

    If a new user or group has just been created, and is about to be used, set ``wait`` to avoid
    the caching of non-existent UIDs or GIDs.
    """
    res = yield from make("/var/yp")
    if res:
        LOG.debug("Updated NIS")
        if wait:
            time.sleep(16)
    return res


@require_host(hosts.LIST)
def configure_mailing_list(name: str) -> Result[Unset]:
    """
    Apply default options to a new mailing list, and create the necessary mail aliases.
    """
    command(["/usr/sbin/config_list", "--inputfile", "/root/mailman-newlist-defaults", name])
    LOG.debug("Configured mailing list: %r", name)
    return Result(State.success)


@require_host(hosts.LIST)
def generate_mailman_aliases() -> Result[Unset]:
    """
    Refresh the Exim alias file for Mailman lists.
    """
    # TODO: Port to SRCFLib, replace with entrypoint.
    command(["/usr/local/sbin/srcf-generate-mailman-aliases"])
    return Result(State.success)


def archive_website(owner: Owner) -> Result[Optional[str]]:
    """
    Rename the web root of a user or society with a timestamp to archive it locally.
    """
    public_html = os.path.join(owner_home(owner, True), "public_html")
    if not os.path.exists(public_html):
        return Result(State.unchanged, None)
    target = "{}_{}".format(public_html, datetime.now().strftime("%Y-%m-%d-%H%M%S"))
    os.rename(public_html, target)
    return Result(State.success, target)


def _archive_files(society: Society, root: str) -> Result[Optional[str]]:
    home = owner_home(society)
    public = owner_home(society, True)
    try:
        os.mkdir(root)
    except FileExistsError:
        pass
    name = "soc-{}-{}.tar.bz2".format(society.society, date.today().strftime("%Y%m%d"))
    target = os.path.join(root, name)
    paths = tuple(filter(os.path.exists, (home, public)))
    if not paths:
        return Result(State.unchanged, None)
    if os.path.exists(target):
        raise FileExistsError(target)
    command(["/bin/tar", "cjf", target, *paths])
    LOG.debug("Archived society files: %r", paths)
    return Result(State.success, target)


def _archive_crontab(society: Society, root: str) -> Result[Optional[str]]:
    crontab = get_crontab(society)
    if not crontab:
        return Result(State.unchanged, None)
    target = os.path.join(root, "crontab")
    with open(target, "w") as f:
        f.write(crontab)
    LOG.debug("Archived crontab: %r", society.society)
    # TOOD: for host in {"cavein", "sinkhole"}: get_crontab(society)
    return Result(State.success, target)


@Result.collect
def archive_society_files(society: Society) -> Collect[None]:
    """
    Create a backup of the society under /archive/societies.
    """
    root = os.path.join("/archive/societies", society.society)
    yield _archive_files(society, root)
    yield _archive_crontab(society, root)
    with open(os.path.join(root, "society_info"), "w") as f:
        f.write(summarise_society(society))


@Result.collect
def delete_files(owner: Owner) -> Collect[None]:
    """
    Remove all public and private files of a member or society.
    """
    home = owner_home(owner)
    public = owner_home(owner, True)
    for path in (home, public):
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            yield Result(State.unchanged)
        else:
            LOG.debug("Deleted files: %r", path)
            yield Result(State.success)


def slay_user(owner: Owner) -> Result[Unset]:
    """
    Kill all processes belonging to the given account.
    """
    try:
        proc = command(["/usr/local/sbin/srcf-slay", owner_name(owner)], output=True)
    except CalledProcessError as ex:
        if ex.returncode == 2:  # User not found.
            return Result(State.unchanged)
        else:
            raise
    else:
        return Result(State.success if proc.stdout else State.unchanged)
