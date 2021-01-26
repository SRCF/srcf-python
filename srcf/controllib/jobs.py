import subprocess
import re
import logging
import os
import time
from datetime import datetime
from contextlib import contextmanager

import psycopg2
import pymysql
from jinja2 import Environment, FileSystemLoader

from srcf import database, pwgen
from srcf.database import schema, queries, Job as db_Job
from srcf.database.schema import Member, Society, Domain
from srcf.mail import send_mail

from srcflib.tasks import mailman, membership

from . import utils


DEFAULT_MAIL_HANDLER = "pip"


emails = Environment(loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), "emails")))
email_headers = {k: emails.get_template("common/header-{0}.txt".format(k)) for k in ("member", "society")}
email_footer = emails.get_template("common/footer.txt").render()


def make_pwd():
    return pwgen().decode("utf-8")


def render_domain_text(domain):
    if any([x[:4] == "xn--" for x in domain.split(".")]):
        # punycode
        return "%s (%s)" % (domain, domain.encode("ascii").decode("idna"))
    else:
        return domain


@contextmanager
def mysql_context(job):
    with open("/root/mysql-root-password", "r") as pwfh:
        rootpw = pwfh.readline().rstrip()
    job.log("Connect to MySQL db")
    conn = pymysql.connect(user="root", host="mysql.internal", passwd=rootpw, db="mysql")
    try:
        yield conn, conn.cursor()
    finally:
        conn.close()


@contextmanager
def pgsql_context(job):
    job.log("Connect to PostgreSQL db")
    # TODO: don't connect to the sysadmins database this way -- it can deadlock with SQLAlchemy.
    # Either allow connections to an alternate database, or connect in a safer way.
    conn = psycopg2.connect(host="postgres.internal", database="sysadmins")
    try:
        yield conn, conn.cursor()
        conn.commit()
    finally:
        conn.close()


def sql_exec(job, cur, desc, sql, *vals):
    job.log(desc)
    try:
        cur.execute(sql, vals)
    except (pymysql.MySQLError, psycopg2.Error) as e:
        raise JobFailed(desc, str(e))


def get_environment():
    return os.getenv("SRCF_JOB_QUEUE")


# Borrowed from srcf-memberdb-cli
def find_admins(admin_crsids, sess):
    admins = (
        sess.query(Member)
        .filter(Member.crsid.in_(admin_crsids))
        .all()
    )
    found = {x.crsid for x in admins}
    missing = set(admin_crsids) - found
    if missing:
        raise KeyError(list(missing)[0])
    return set(admins)


def subproc_call(job, desc, cmd, stdin=None):
    job.log(desc)
    pipe = subprocess.Popen(cmd, stdin=(subprocess.PIPE if stdin else None), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out, _ = pipe.communicate(stdin)
    if pipe.returncode:
        raise JobFailed(desc, out or None)
    if out:
        try:
            out = out.decode("utf-8")
        except UnicodeDecodeError:
            pass
        job.log(desc, "output", raw=out)


def make_public_dir(job, root, user, dirname, uid, gid):
    dir_path = os.path.join("/public", root, user, dirname)
    link_path = os.path.join("/", root, user, dirname)
    job.log("Create and link " + dirname + " directory")
    os.mkdir(dir_path)
    os.symlink(dir_path, link_path)
    # Only the first attempt to chown to a new user needs to be wrapped in
    # utils.nfs_aware_chown, and we already chowned the home directory
    os.chown(dir_path, uid, gid)
    os.lchown(link_path, uid, gid)
    os.chmod(dir_path, 0o2775)


def update_nis(job, wait_netapp=False):
    subproc_call(job, "Update NIS maps", ["make", "-C", "/var/yp"])
    if wait_netapp:
        # NetApp processes pushes asynchronously and I know of no way to find out when it's finished :-(
        # We only need to wait for it if we're creating a user/group and need to immediately use it on NFS.
        job.log("Waiting for NIS servers to process the map update")
        time.sleep(16)


def render_email(target, template, **kwargs):
    target_type = "member" if isinstance(target, Member) else "society"
    content = "\n\n".join([
        email_headers[target_type].render(target=target),
        emails.get_template(target_type + "/" + template + ".txt").render(target=target, **kwargs),
        email_footer])
    return content


def mail_users(target, subject, template, **kwargs):
    target_type = "member" if isinstance(target, Member) else "society"
    to = (target.name if target_type == "member" else target.description, target.email)
    subject = "[SRCF] " + (target.society + ": " if target_type == "society" else "") + subject
    content = render_email(target, template, **kwargs)
    send_mail(to, subject, content, copy_sysadmins=False)


all_jobs = {}


def add_job(cls):
    all_jobs[cls.JOB_TYPE] = cls
    return cls


class JobFailed(Exception):
    def __init__(self, message=None, raw=None):
        self.message = message
        self.raw = raw


class Job(object):
    def __init__(self, row):
        self.row = row
        self.type = row.type

    @staticmethod
    def of_row(row):
        if row.type in all_jobs:
            return all_jobs[row.type](row)
        else:
            return Job(row)

    @classmethod
    def find_by_user(cls, sess, crsid):
        job_row = db_Job
        jobs = (
            sess.query(job_row)
            .filter(job_row.owner_crsid == crsid,
                    ~job_row.args.defined("society"))
            .order_by(job_row.job_id.desc())
        )
        return [Job.of_row(r) for r in jobs]

    @classmethod
    def find_by_society(cls, sess, name):
        job_row = db_Job
        d = {"society": name}
        jobs = (
            sess.query(job_row)
            .filter(job_row.args.contains(d))
            .order_by(job_row.job_id.desc())
        )
        return [Job.of_row(r) for r in jobs]

    @classmethod
    def find(cls, sess, id):
        job = sess.query(database.Job).get(id)
        if not job:
            return None
        else:
            job = cls.of_row(job)
            job.resolve_references(sess)
            return job

    def resolve_references(self, sess):
        """
        Due to jobs having a varying number of arguments, and hstore columns
        mapping strings to strings, sometimes we'll store (say) a string crsid
        for the target of a job (say, adding an admin).

        This function uses `sess` to look up those Members/Societies and
        populate attributes with `srcf.database.*` objects.

        It would be far nice if SQLAlchemy could handle this, even using a JOIN
        where possible, but this sounds like a lot of work.
        """
        pass

    def visible_to(self, crsid):
        return self.owner_crsid and self.owner_crsid == crsid

    @classmethod
    def create(cls, owner, args, require_approval):
        return cls(database.Job(
            type=cls.JOB_TYPE,
            created_at=datetime.now(),
            owner=owner,
            state="unapproved" if require_approval else "queued",
            args=args,
            environment=get_environment()
        ))

    def log(self, msg="", type="progress", level=logging.DEBUG, raw=None, **kwargs):
        self.logger.log(level, msg, extra={"task": "{0}/{1} {2}".format(self.job_id, self.JOB_TYPE, type),
                                           "job_id": self.job_id, "type": type, "raw": raw}, **kwargs)

    def run(self, sess):
        """Run the job. `self.state` will be set to `done` or `failed`."""
        raise JobFailed("not implemented")

    job_id = property(lambda s: s.row.job_id)
    created_at = property(lambda s: s.row.created_at)
    owner = property(lambda s: s.row.owner)
    owner_crsid = property(lambda s: s.row.owner_crsid)
    state = property(lambda s: s.row.state)
    state_message = property(lambda s: s.row.state_message)

    @state.setter
    def state(s, n): s.row.state = n
    @state_message.setter
    def state_message(s, n): s.row.state_message = n

    def set_state(self, state, message=None):
        self.state = state
        self.state_message = message

    def __repr__(self): return "<Unknown {0.type}>".format(self)
    def __str__(self): return "Unknown job type: {0.type}".format(self)


class SocietyJob(Job):
    society_society = property(lambda s: s.row.args["society"])

    def resolve_references(self, sess):
        super(SocietyJob, self).resolve_references(sess)
        try:
            self.society = queries.get_society(self.society_society)
        except KeyError:
            # maybe the society doesn't exist yet / any more
            self.society = None

    def visible_to(self, crsid):
        return super(SocietyJob, self).visible_to(crsid) or self.society and crsid in self.society


# Test job - takes a long time to test for concurrency issues
@add_job
class Test(Job):
    JOB_TYPE = 'test'

    def __init__(self, row):
        self.row = row

    def visible_to(self, crsid):
        return self.owner.crsid == crsid

    @classmethod
    def new(cls, mem, sleep_time):
        args = {
            "sleep_time": str(sleep_time)
        }
        return cls.create(mem, args, require_approval=False)

    sleep_time = property(lambda s: min(40, int(s.row.args["sleep_time"])))

    def run(self, sess):
        time.sleep(self.sleep_time)

    def __repr__(self): return "<Test {0.owner.crsid}>".format(self)
    def __str__(self): return "Test: {0.owner.crsid} {0.sleep_time}".format(self)


@add_job
class Signup(Job):
    JOB_TYPE = 'signup'

    def __init__(self, row):
        self.row = row

    def visible_to(self, crsid):
        return self.crsid == crsid

    @classmethod
    def new(cls, crsid, preferred_name, surname, email, social, mail_handler=DEFAULT_MAIL_HANDLER):
        args = {
            "crsid": crsid,
            "preferred_name": preferred_name,
            "surname": surname,
            "email": email,
            "mail_handler": mail_handler,
            "social": "y" if social else "n"
        }
        try:
            utils.ldapsearch(crsid)
        except KeyError:
            require_approval = True
        else:
            require_approval = False
        return cls.create(None, args, require_approval)

    crsid = property(lambda s: s.row.args["crsid"])
    preferred_name = property(lambda s: s.row.args["preferred_name"])
    surname = property(lambda s: s.row.args["surname"])
    email = property(lambda s: s.row.args["email"])
    mail_handler = property(lambda s: s.row.args.get("mail_handler", DEFAULT_MAIL_HANDLER))
    social = property(lambda s: s.row.args["social"] == "y")

    def run(self, sess):
        crsid = self.crsid

        self.log("Sanity check for an existing account for {0}".format(crsid))
        if queries.list_members().get(crsid):
            raise JobFailed(crsid + " is already a user")

        name = (self.preferred_name + " " + self.surname).strip()

        self.log("Create memberdb entry")
        member = Member(crsid=self.crsid,
                        preferred_name=self.preferred_name,
                        surname=self.surname,
                        email=self.email,
                        mail_handler=self.mail_handler,
                        member=True,
                        user=True)
        sess.add(member)
        sess.commit()

        uid = member.uid
        gid = member.gid

        home_path = os.path.join("/home", crsid)
        public_path = os.path.join("/public", "home", crsid)

        # NB: adduser --uid will implicitly use gid=uid; --gid does not do what we want (bypasses group creation)
        # NB: adduser --system doesn't call adduser.local (we do all the work here)
        subproc_call(self, "Add UNIX user (uid %d)" % uid, ["adduser", "--no-create-home", "--disabled-password",
                                                            "--system", "--uid", str(uid), "--group",
                                                            "--shell", "/bin/bash", "--gecos", name, crsid])

        password = make_pwd()
        chpasswd_data = ("%s:%s" % (crsid, password)).encode("ascii")
        subproc_call(self, "Set password", ["chpasswd"], stdin=chpasswd_data)

        update_nis(self, wait_netapp=True)

        self.log("Create home and public directories")
        for path, perm in ((home_path, 0o2770), (public_path, 0o2775)):
            os.mkdir(path)
            os.chmod(path, perm)
            utils.nfs_aware_chown(path, uid, gid)

        subproc_call(self, "Set ACL on home directory", ["/usr/bin/nfs4_setfacl", "-a", "A::Debian-exim@srcf.net:RX", home_path])

        self.log("Populate home directory from /etc/skel")
        utils.copytree_chown_chmod("/etc/skel", home_path, uid, gid)

        make_public_dir(self, "home", crsid, "public_html", uid, gid)

        subproc_call(self, "Update quotas", ["/usr/local/sbin/srcf-update-quotas", crsid])

        if self.mail_handler == "pip":
            self.log("Create default .forward file")
            path = "/home/" + crsid + "/.forward"
            f = open(path, "w")
            f.write(self.email + "\n")
            f.close()

            self.log("Set correct permissions on .forward file")
            os.chown(path, uid, gid)

        subproc_call(self, "Update Apache groups", ["/usr/local/sbin/srcf-updateapachegroups"])
        ml_entry = '"{name}" <{email}>'.format(name=name, email=self.email)
        subproc_call(self, "Queue mail subscriptions", ["/usr/local/sbin/srcf-enqueue-mlsub",
                                                        "soc-srcf-maintenance:" + ml_entry,
                                                        ("soc-srcf-social:" + ml_entry) if self.social else ""])
        subproc_call(self, "Export memberdb", ["/usr/local/sbin/srcf-memberdb-export"])

        self.log("Create legacy mailbox")
        send_mail((False, "real-%s@srcf.net" % crsid), "Welcome to your SRCF inbox",
                  render_email(member, "mailbox-placeholder"), copy_sysadmins=False)

        self.log("Send welcome email")
        mail_users(member, "Your SRCF account", "signup", password=password)

    def __repr__(self): return "<Signup {0.crsid}>".format(self)
    def __str__(self): return "Signup: {0.crsid} ({0.preferred_name} {0.surname}, {0.email}, {0.mail_handler} mail)".format(self)


@add_job
class Reactivate(Job):
    JOB_TYPE = 'reactivate'

    def __init__(self, row):
        self.row = row

    @classmethod
    def new(cls, member, email):
        args = {"email": email}
        return cls.create(member, args, True)

    email = property(lambda s: s.row.args["email"])

    def __repr__(self): return "<Reactivate {0.owner_crsid}>".format(self)
    def __str__(self): return "Reactivate user: {0.owner.crsid} ({0.email})".format(self)

    def run(self, sess):
        crsid = self.owner.crsid
        password = make_pwd()

        old_email = self.owner.email
        self.log("Update email address")
        self.owner.email = self.email
        self.log("Update member/user status")
        self.owner.member = True
        self.owner.user = True

        subproc_call(self, "Re-enable UNIX user", ["/usr/sbin/usermod", "-s", "/bin/bash", "-e", "", crsid])
        subproc_call(self, "Change UNIX password for {0}".format(crsid), ["/usr/sbin/chpasswd"], (crsid + ":" + password).encode("utf-8"))
        update_nis(self)

        self.log("Check existing .forward file")
        path = "/home/" + crsid + "/.forward"
        try:
            with open(path, "r") as f:
                forward_email = f.read().rstrip()
        except OSError:
            pass
        else:
            if forward_email == old_email:
                self.log("Update .forward file")
                with open(path, "w") as f:
                    f.write(self.email + "\n")

        self.log("Send confirmation")
        mail_users(self.owner, "Account reactivated", "reactivate", new_email=self.email, password=password)


@add_job
class ResetUserPassword(Job):
    JOB_TYPE = 'reset_user_password'

    def __init__(self, row):
        self.row = row

    @classmethod
    def new(cls, member):
        require_approval = member.danger
        return cls.create(member, {}, require_approval)

    def run(self, sess):
        crsid = self.owner.crsid
        password = make_pwd()

        subproc_call(self, "Change UNIX password for {0}".format(crsid), ["/usr/sbin/chpasswd"], (crsid + ":" + password).encode("utf-8"))
        update_nis(self)

        self.log("Send new password")
        mail_users(self.owner, "Password reset", "srcf-password", password=password)

    def __repr__(self): return "<ResetUserPassword {0.owner_crsid}>".format(self)
    def __str__(self): return "Reset user password: {0.owner.crsid} ({0.owner.name})".format(self)


@add_job
class UpdateName(Job):
    JOB_TYPE = 'update_name'

    def __init__(self, row):
        self.row = row

    @classmethod
    def new(cls, member, preferred_name, surname):
        args = {"preferred_name": preferred_name,
                "surname": surname}
        require_approval = member.danger
        return cls.create(member, args, require_approval)

    preferred_name = property(lambda s: s.row.args["preferred_name"])
    surname = property(lambda s: s.row.args["surname"])
    name = property(lambda s: "{} {}".format(s.preferred_name, s.surname))

    def __repr__(self): return "<UpdateName {0.owner_crsid}>".format(self)
    def __str__(self): return "Update name: {0.owner.crsid} ({0.name})".format(self)

    def run(self, sess):
        old_name = self.owner.name
        self.log("Update name")
        membership.update_member_name(self.owner, self.preferred_name, self.surname)

        self.log("Send confirmation")
        mail_users(self.owner, "Name updated", "name", old_name=old_name, new_name=self.name)


@add_job
class UpdateEmailAddress(Job):
    JOB_TYPE = 'update_email_address'

    def __init__(self, row):
        self.row = row

    @classmethod
    def new(cls, member, email):
        args = {"email": email}
        require_approval = member.danger
        return cls.create(member, args, require_approval)

    email = property(lambda s: s.row.args["email"])

    def __repr__(self): return "<UpdateEmailAddress {0.owner_crsid}>".format(self)
    def __str__(self): return "Update email address: {0.owner.crsid} ({0.email})".format(self)

    def run(self, sess):
        old_email = self.owner.email
        self.log("Update email address")
        self.owner.email = self.email

        self.log("Check existing .forward file")
        path = "/home/" + self.owner.crsid + "/.forward"
        try:
            with open(path, "r") as f:
                forward_email = f.read().rstrip()
        except OSError:
            pass
        else:
            if forward_email == old_email:
                self.log("Update .forward file")
                with open(path, "w") as f:
                    f.write(self.email + "\n")

        self.log("Send confirmation")
        mail_users(self.owner, "Email address updated", "email", old_email=old_email, new_email=self.email)


@add_job
class UpdateMailHandler(Job):
    JOB_TYPE = 'update_mail_handler'

    def __init__(self, row):
        self.row = row

    @classmethod
    def new(cls, member, mail_handler):
        if mail_handler not in schema.VALID_MAIL_HANDLERS:
            raise LookupError(mail_handler)
        args = {"mail_handler": mail_handler}
        require_approval = member.danger
        return cls.create(member, args, require_approval)

    mail_handler = property(lambda s: s.row.args["mail_handler"])

    def __repr__(self): return "<UpdateMailHandler {0.owner_crsid}>".format(self)
    def __str__(self): return "Update email handler: {0.owner.crsid} ({0.mail_handler})".format(self)

    def run(self, sess):
        self.log("Update email handler")
        self.owner.mail_handler = self.mail_handler


@add_job
class CreateUserMailingList(Job):
    JOB_TYPE = 'create_user_mailing_list'

    def __init__(self, row):
        self.row = row

    @classmethod
    def new(cls, member, listname):
        args = {"listname": listname}
        require_approval = member.danger
        return cls.create(member, args, require_approval)

    listname = property(lambda s: s.row.args["listname"])

    def __repr__(self): return "<CreateUserMailingList {0.owner_crsid}-{0.listname}>".format(self)
    def __str__(self): return "Create user mailing list: {0.owner_crsid}-{0.listname}".format(self)

    def run(self, sess):
        self.log("Sanity check list name")
        if (not re.match(r"^[A-Za-z0-9\-]+$", self.listname)
            or self.listname.split("-")[-1] in ("admins", "admin", "bounces", "confirm", "join", "leave",
                                                "owner", "request", "subscribe", "unsubscribe")):
            raise JobFailed("Invalid list suffix {}".format(self.listname))

        self.log("Create list")
        result = mailman.create_list(self.owner, self.listname)

        self.log("Send password")
        full_listname, password = result.value
        mail_users(self.owner, "Mailing list created", "list-create", listname=full_listname, password=password)


@add_job
class ResetUserMailingListPassword(Job):
    JOB_TYPE = 'reset_user_mailing_list_password'

    def __init__(self, row):
        self.row = row

    @classmethod
    def new(cls, member, listname):
        args = {"listname": listname}
        require_approval = member.danger
        return cls.create(member, args, require_approval)

    listname = property(lambda s: s.row.args["listname"])

    def __repr__(self): return "<ResetUserMailingListPassword {0.listname}>".format(self)
    def __str__(self): return "Reset user mailing list password: {0.listname}".format(self)

    def run(self, sess):
        # TODO: allow name input
        try:
            owner, mlist = self.listname.split("-", 1)
        except ValueError:
            owner = self.listname
            mlist = None
        assert owner == self.owner.crsid

        self.log("Reset owner and password")
        result = mailman.reset_owner_password(self.owner, mlist)

        self.log("Send new password")
        mail_users(self.owner, "Mailing list password reset", "list-password", listname=self.listname, password=result.value)


@add_job
class AddUserVhost(Job):
    JOB_TYPE = 'add_user_vhost'

    def __init__(self, row):
        self.row = row
        self.domain_text = render_domain_text(self.domain)

    @classmethod
    def new(cls, member, domain, root):
        root = "public_html/{}".format(root) if root else None
        args = {"domain": domain, "root": root}
        # TODO: We don't validate whether a user is allowed a given vhost, which extends to
        # subdomains of other users' domains / wildcard, other srcf.net domains etc.
        require_approval = True  # member.danger
        return cls.create(member, args, require_approval)

    domain = property(lambda s: s.row.args["domain"])
    root = property(lambda s: s.row.args["root"])

    def __repr__(self): return "<AddUserVhost {0.owner_crsid} {0.domain}>".format(self)
    def __str__(self): return "Add custom domain: {0.owner.crsid} ({0.domain_text} -> {0.root})".format(self)

    def run(self, sess):
        self.log("Add domain entry")
        sess.add(Domain(class_="user",
                        owner=self.owner_crsid,
                        domain=self.domain,
                        root=self.root,
                        wild=False))

        self.log("Send confirmation")
        mail_users(self.owner, "Custom domain added", "add-vhost", domain=self.domain_text, root=self.root)


@add_job
class ChangeUserVhostDocroot(Job):
    JOB_TYPE = 'change_user_vhost_docroot'

    def __init__(self, row):
        self.row = row
        self.domain_text = render_domain_text(self.domain)

    @classmethod
    def new(cls, member, domain, root):
        root = "public_html/{}".format(root) if root else None
        args = {"domain": domain, "root": root}
        require_approval = member.danger
        return cls.create(member, args, require_approval)

    domain = property(lambda s: s.row.args["domain"])
    root = property(lambda s: s.row.args["root"])

    def __repr__(self): return "<ChangeUserVhostDocroot {0.owner_crsid} {0.domain}>".format(self)
    def __str__(self): return "Change custom domain root: {0.owner.crsid} ({0.domain_text} -> {0.root})".format(self)

    def run(self, sess):
        self.log("Change domain entry")
        results = sess.query(Domain).filter(Domain.class_ == "user",
                                            Domain.owner == self.owner_crsid,
                                            Domain.domain == self.domain).all()

        if not results:
            raise JobFailed("{0.domain} does not exist", self)
        elif len(results) > 1:
            raise JobFailed("Multiple entries for {0.domain}", self)
        domain = results[0]

        domain.root = self.root
        sess.add(domain)

        self.log("Send confirmation")
        mail_users(self.owner, "Custom domain document root changed", "change-vhost-docroot", domain=self.domain_text, root=self.root)


@add_job
class RemoveUserVhost(Job):
    JOB_TYPE = 'remove_user_vhost'

    def __init__(self, row):
        self.row = row
        self.domain_text = render_domain_text(self.domain)

    @classmethod
    def new(cls, member, domain):
        args = {"domain": domain}
        require_approval = member.danger
        return cls.create(member, args, require_approval)

    domain = property(lambda s: s.row.args["domain"])

    def __repr__(self): return "<RemoveUserVhost {0.owner_crsid} {0.domain}>".format(self)
    def __str__(self): return "Remove custom domain: {0.owner.crsid} ({0.domain})".format(self)

    def run(self, sess):
        self.log("Lookup domain entry")
        try:
            domain = sess.query(Domain).filter(Domain.domain == self.domain)[0]
        except IndexError:
            raise JobFailed("{0.domain} does not exist".format(self))
        if not domain.owner == self.owner_crsid:
            raise JobFailed("{0.domain} is not owned by {0.owner_crsid}".format(self))

        self.log("Remove domain entry")
        sess.delete(domain)

        self.log("Send confirmation")
        mail_users(self.owner, "Custom domain removed", "remove-vhost", domain=self.domain_text)


@add_job
class CreateSociety(SocietyJob):
    JOB_TYPE = 'create_society'

    def __init__(self, row):
        self.row = row

    def resolve_references(self, sess):
        super(CreateSociety, self).resolve_references(sess)
        self.admins = (
            sess.query(database.Member)
            .filter(database.Member.crsid.in_(self.admin_crsids))
            .all()
        )
        if len(self.admins) != len(self.admin_crsids):
            raise KeyError("CreateSociety references admins")

    @classmethod
    def new(cls, member, society, description, admins):
        args = {
            "society": society,
            "description": description,
            "admins": ",".join(a for a in admins),
        }
        return cls.create(member, args, True)

    description = property(lambda s: s.row.args["description"])
    admin_crsids = property(lambda s: s.row.args["admins"].split(","))

    def run(self, sess):
        self.log("Create memberdb entry")
        soc = Society(society=self.society_society,
                      description=self.description,
                      admins=find_admins(self.admin_crsids, sess))
        sess.add(soc)
        sess.commit()

        uid = soc.uid
        gid = soc.gid

        subproc_call(self, "Add group (gid %d)" % gid, ["/usr/sbin/addgroup", "--gid", str(gid),
                                                        "--force-badname", self.society_society])

        home_path = os.path.join("/societies", self.society_society)
        public_path = os.path.join("/public", "societies", self.society_society)

        for admin in self.admin_crsids:
            subproc_call(self, "Add user {0} to group".format(admin), ["/usr/sbin/adduser", admin, self.society_society])

            self.log("Create society home symlink for {0}".format(admin))
            try:
                os.symlink(home_path, os.path.join("/home", admin, self.society_society))
            except Exception:
                pass

        subproc_call(self, "Add society user (uid %d)" % uid, ["/usr/sbin/adduser", "--force-badname", "--no-create-home",
                                                               "--uid", str(uid), "--gid", str(gid), "--gecos", self.description,
                                                               "--disabled-password", "--system", self.society_society])
        subproc_call(self, "Set home directory", ["/usr/sbin/usermod", "-d", home_path, self.society_society])

        update_nis(self, wait_netapp=True)

        self.log("Create home and public directories")
        for path, perm in ((home_path, 0o2770), (public_path, 0o2775)):
            os.mkdir(path)
            os.chmod(path, perm)
            utils.nfs_aware_chown(path, uid, gid)

        subproc_call(self, "Set ACL on home directory", ["/usr/bin/nfs4_setfacl", "-a", "A::Debian-exim@srcf.net:RX", home_path])

        for name in ("public_html", "cgi-bin"):
            make_public_dir(self, "societies", self.society_society, name, uid, gid)

        subproc_call(self, "Update quotas", ["/usr/local/sbin/srcf-update-quotas", self.society_society])
        subproc_call(self, "Generate sudoers", ["/usr/local/sbin/srcf-generate-society-sudoers"])
        subproc_call(self, "Export memberdb", ["/usr/local/sbin/srcf-memberdb-export"])

        self.log("Send welcome email")
        newsoc = queries.get_society(self.society_society)
        mail_users(newsoc, "New shared account created", "signup")

    def __repr__(self): return "<CreateSociety {0.society_society}>".format(self)
    def __str__(self): return "Create society: {0.society_society} ({0.description})".format(self)


@add_job
class UpdateSocietyDescription(SocietyJob):
    JOB_TYPE = 'update_society_description'

    def __init__(self, row):
        self.row = row

    @classmethod
    def new(cls, requesting_member, society, description):
        args = {
            "society": society.society,
            "description": description
        }
        require_approval = requesting_member.danger or society.danger
        return cls.create(requesting_member, args, require_approval)

    description = property(lambda s: s.row.args["description"])

    def __repr__(self): return "<UpdateSocietyDescription {0.society_society}>".format(self)
    def __str__(self): return "Update society description: {0.society_society} ({0.description})".format(self)

    def run(self, sess):
        old_description = self.society.description
        self.log("Update description")
        membership.update_society_description(self.society, self.description)

        self.log("Send confirmation")
        mail_users(self.society, "Description updated", "description",
                   old_description=old_description, new_description=self.description)


@add_job
class UpdateSocietyRoleEmail(SocietyJob):
    JOB_TYPE = 'update_society_role_email'

    def __init__(self, row):
        self.row = row

    @classmethod
    def new(cls, requesting_member, society, email):
        args = {
            "society": society.society,
            "email": email
        }
        require_approval = (requesting_member.danger or society.danger or
                            email.endswith(("@cam.ac.uk", "@hermes.cam.ac.uk")))
        return cls.create(requesting_member, args, require_approval)

    email = property(lambda s: s.row.args["email"])

    def __repr__(self): return "<UpdateSocietyRoleEmail {0.society_society} {0.email}>".format(self)
    def __str__(self): return "Update society role email: {0.society_society} ({0.email})".format(self)

    def run(self, sess):
        old_email = self.society.role_email
        self.log("Update email address")
        self.society.role_email = self.email

        self.log("Send confirmation")
        mail_users(self.society, "Role email updated", "role-email", old_email=old_email, new_email=self.email)


@add_job
class ChangeSocietyAdmin(SocietyJob):
    JOB_TYPE = 'change_society_admin'

    def __init__(self, row):
        self.row = row

    def resolve_references(self, sess):
        super(ChangeSocietyAdmin, self).resolve_references(sess)
        self.target_member = queries.get_member(self.target_member_crsid)

    @classmethod
    def new(cls, requesting_member, society, target_member, action):
        if action not in {"add", "remove"}:
            raise ValueError("action should be 'add' or 'remove'", action)
        args = {
            "society": society.society,
            "target_member": target_member.crsid,
            "action": action
        }
        require_approval = (
            society.danger
            or target_member.danger
            or requesting_member.danger
            or (action == "remove"
                and len(society.admin_crsids) == 1
                and society.role_email))
        return cls.create(requesting_member, args, require_approval)

    target_member_crsid = property(lambda s: s.row.args["target_member"])
    action = property(lambda s: s.row.args["action"])

    def __repr__(self):
        return "<ChangeSocietyAdmin {0.action} {0.society_society} {0.target_member_crsid}>".format(self)

    def __str__(self):
        verb = self.action.title()
        prep = "to" if self.action == "add" else "from"
        fmt = "{verb} society admin: {0.target_member.crsid} ({0.target_member.name}) {prep} {0.society_society}"
        return fmt.format(self, verb=verb, prep=prep)

    def add_admin(self, sess):
        if self.target_member in self.society.admins:
            raise JobFailed("{0.target_member.crsid} is already an admin of {0.society}".format(self))
        if not self.target_member.member:
            raise JobFailed("{0.target_member.crsid} is not a SRCF member".format(self))
        if not self.target_member.user:
            raise JobFailed("{0.target_member.crsid} is not a SRCF user".format(self))

        self.society.admins.add(self.target_member)

        subproc_call(self, "Add user to group", ["adduser", self.target_member.crsid, self.society.society])

        target_ln = "/home/{0.target_member.crsid}/{0.society.society}".format(self)
        source_ln = "/societies/{0.society.society}/".format(self)
        if not os.path.exists(target_ln):
            self.log("Create society home symlink")
            os.symlink(source_ln, target_ln)

        update_nis(self)

        self.log("Send confirmation to new member")
        mail_users(self.target_member, "Access granted to " + self.society_society, "add-admin", society=self.society)
        self.log("Send confirmation to the rest")
        adminNames = sorted("{0.name} ({0.crsid})".format(m) for m in self.society.admins)
        mail_users(self.society, "Access granted for " + self.target_member.crsid, "add-admin",
                   added=self.target_member, requester=self.owner, admins="\n".join(adminNames))

    def rm_admin(self, sess):
        if self.target_member not in self.society.admins:
            raise JobFailed("{0.target_member.crsid} is not an admin of {0.society.society}".format(self))

        if len(self.society.admins) == 1:
            raise JobFailed("Removing all admins not implemented")

        self.society.admins.remove(self.target_member)
        subproc_call(self, "Remove user from group", ["deluser", self.target_member.crsid, self.society.society])

        target_ln = "/home/{0.target_member.crsid}/{0.society.society}".format(self)
        source_ln = "/societies/{0.society.society}/".format(self)
        if os.path.islink(target_ln) and os.path.samefile(target_ln, source_ln):
            self.log("Remove society home symlink")
            os.remove(target_ln)

        update_nis(self)

        self.log("Send confirmation to remaining admins")
        adminNames = sorted("{0.name} ({0.crsid})".format(m) for m in self.society.admins)
        mail_users(self.society, "Access removed for " + self.target_member.crsid, "remove-admin",
                   removed=self.target_member, requester=self.owner, admins="\n".join(adminNames))
        self.log("Send confirmation to removed member")
        mail_users(self.target_member, "Access removed from " + self.society_society, "remove-admin", society=self.society)

    def run(self, sess):
        if self.owner not in self.society.admins:
            raise JobFailed("{0.owner.crsid} is not permitted to change the admins of {0.society.society}".format(self))

        if self.action == "add":
            self.add_admin(sess)
        else:
            self.rm_admin(sess)


@add_job
class CreateSocietyMailingList(SocietyJob):
    JOB_TYPE = 'create_society_mailing_list'

    def __init__(self, row):
        self.row = row

    @classmethod
    def new(cls, member, society, listname):
        args = {
            "society": society.society,
            "listname": listname
        }
        require_approval = member.danger or society.danger
        return cls.create(member, args, require_approval)

    listname = property(lambda s: s.row.args["listname"])

    def __repr__(self): return "<CreateSocietyMailingList {0.society_society}-{0.listname}>".format(self)
    def __str__(self): return "Create society mailing list: {0.society_society}-{0.listname}".format(self)

    def run(self, sess):
        self.log("Sanity check list name")
        if (not re.match(r"^[A-Za-z0-9\-]+$", self.listname)
            or self.listname.split("-")[-1] in ("admins", "admin", "bounces", "confirm", "join", "leave",
                                                "owner", "request", "subscribe", "unsubscribe")):
            raise JobFailed("Invalid list suffix {}".format(self.listname))

        self.log("Create list")
        result = mailman.create_list(self.society, self.listname)

        self.log("Send password")
        full_listname, password = result.value
        mail_users(self.society, "Mailing list created", "list-create", listname=full_listname, password=password, requester=self.owner)


@add_job
class ResetSocietyMailingListPassword(SocietyJob):
    JOB_TYPE = 'reset_society_mailing_list_password'

    def __init__(self, row):
        self.row = row

    @classmethod
    def new(cls, member, society, listname):
        args = {
            "society": society.society,
            "listname": listname,
        }
        require_approval = member.danger or society.danger
        return cls.create(member, args, require_approval)

    listname = property(lambda s: s.row.args["listname"])

    def __repr__(self): return "<ResetSocietyMailingListPassword {0.listname}>".format(self)
    def __str__(self): return "Reset society mailing list password: {0.listname}".format(self)

    def run(self, sess):
        # TODO: allow name input
        try:
            owner, mlist = self.listname.split("-", 1)
        except ValueError:
            owner = self.listname
            mlist = None
        assert owner == self.society.society

        self.log("Reset owner and password")
        result = mailman.reset_owner_password(self.society, mlist)

        self.log("Send new password")
        mail_users(self.society, "Mailing list password reset", "list-password", listname=self.listname, password=result.value, requester=self.owner)

# Here be dragons: we trust the value of crsid a *lot* (such that it appears unescaped in SQL queries).
# Quote with backticks and ensure only valid characters (alnum for crsid, alnum + [_-] for society).


@add_job
class CreateMySQLUserDatabase(Job):
    JOB_TYPE = 'create_mysql_user_database'

    def __init__(self, row):
        self.row = row

    @classmethod
    def new(cls, member):
        require_approval = member.danger
        return cls.create(member, {}, require_approval)

    def run(self, sess):
        crsid = self.owner.crsid
        assert crsid.isalnum()

        password = make_pwd()

        with mysql_context(self) as (db, cursor):
            sql_exec(self, cursor, "Create database",         "CREATE DATABASE " + crsid)
            sql_exec(self, cursor, "Grant privileges (base)", "GRANT ALL PRIVILEGES ON `" + crsid + "`.*    to '" + crsid + "'@'%%'")
            sql_exec(self, cursor, "Grant privileges (wild)", "GRANT ALL PRIVILEGES ON `" + crsid + "/%%`.* to '" + crsid + "'@'%%'")
            sql_exec(self, cursor, "Set password",            "SET PASSWORD FOR '" + crsid + "'@'%%' = %s", password)

        self.log("Send password")
        mail_users(self.owner, "MySQL database created", "mysql-create", password=password)

    def __repr__(self): return "<CreateMySQLUserDatabase {0.owner_crsid}>".format(self)
    def __str__(self): return "Create user MySQL database: {0.owner.crsid} ({0.owner.name})".format(self)


@add_job
class ResetMySQLUserPassword(Job):
    JOB_TYPE = 'reset_mysql_user_password'

    # NB: also used to create a MySQL user (in cases where a database doesn't need to be created)

    def __init__(self, row):
        self.row = row

    @classmethod
    def new(cls, member):
        require_approval = member.danger
        return cls.create(member, {}, require_approval)

    def run(self, sess):
        crsid = self.owner.crsid
        assert crsid.isalnum()

        password = make_pwd()

        with mysql_context(self) as (db, cursor):
            sql_exec(self, cursor, "(Re-)grant privileges (base)", "GRANT ALL PRIVILEGES ON `" + crsid + "`.*    to '" + crsid + "'@'%%'")
            sql_exec(self, cursor, "(Re-)grant privileges (wild)", "GRANT ALL PRIVILEGES ON `" + crsid + "/%%`.* to '" + crsid + "'@'%%'")
            sql_exec(self, cursor, "Reset password", "SET PASSWORD FOR '" + crsid + "'@'%%' = %s", password)

        self.log("Send new password")
        mail_users(self.owner, "MySQL database password reset", "mysql-password", password=password)

    def __repr__(self): return "<ResetMySQLUserPassword {0.owner_crsid}>".format(self)
    def __str__(self): return "Reset user MySQL password: {0.owner.crsid} ({0.owner.name})".format(self)


@add_job
class CreateMySQLSocietyDatabase(SocietyJob):
    JOB_TYPE = 'create_mysql_society_database'

    def __init__(self, row):
        self.row = row

    @classmethod
    def new(cls, member, society):
        args = {"society": society.society}
        require_approval = society.danger or member.danger
        return cls.create(member, args, require_approval)

    def run(self, sess):
        crsid = self.owner.crsid
        assert crsid.isalnum()
        socname = self.society_society
        assert utils.is_valid_socname(socname)

        password = make_pwd()
        usrpassword = None

        with mysql_context(self) as (db, cursor):
            sql_exec(self, cursor, "Create society database", "CREATE DATABASE " + socname)

            sql_exec(self, cursor, "Check for existing owner user", "SELECT EXISTS (SELECT DISTINCT User FROM mysql.user WHERE User = %s) AS e", self.owner.crsid)
            if cursor.fetchone()[0] == 0:
                usrpassword = make_pwd()

            sql_exec(self, cursor, "Grant privileges (society, base)", "GRANT ALL PRIVILEGES ON `" + socname + "`.*   TO '" + socname + "'@'%%'")
            sql_exec(self, cursor, "Grant privileges (society, wild)", "GRANT ALL PRIVILEGES ON `" + socname + "/%%`.* TO '" + socname + "'@'%%'")
            sql_exec(self, cursor, "Grant privileges (user, base)",    "GRANT ALL PRIVILEGES ON `" + socname + "`.*   TO '" + self.owner.crsid + "'@'%%'")
            sql_exec(self, cursor, "Grant privileges (user, wild)",    "GRANT ALL PRIVILEGES ON `" + socname + "/%%`.* TO '" + self.owner.crsid + "'@'%%'")
            sql_exec(self, cursor, "Set society user password",        "SET PASSWORD FOR '" + socname + "'@'%%' = %s", password)

            if usrpassword is not None:
                sql_exec(self, cursor, "Set owner user password", "SET PASSWORD FOR " + self.owner.crsid + "@'%%' = %s", usrpassword)

        self.log("Send society password")
        mail_users(self.society, "MySQL database created", "mysql-create", password=password, requester=self.owner)
        if usrpassword:
            self.log("Send owner password")
            mail_users(self.owner, "MySQL account created", "mysql-account", password=usrpassword, database=self.society)

    def __repr__(self): return "<CreateMySQLSocietyDatabase {0.society_society}>".format(self)
    def __str__(self): return "Create society MySQL database: {0.society_society}".format(self)


@add_job
class ResetMySQLSocietyPassword(SocietyJob):
    JOB_TYPE = 'reset_mysql_society_password'

    def __init__(self, row):
        self.row = row

    @classmethod
    def new(cls, member, society):
        args = {"society": society.society}
        require_approval = society.danger or member.danger
        return cls.create(member, args, require_approval)

    def run(self, sess):
        crsid = self.owner.crsid
        assert crsid.isalnum()
        socname = self.society_society
        assert utils.is_valid_socname(socname)

        password = make_pwd()

        with mysql_context(self) as (db, cursor):
            sql_exec(
                self, cursor,
                "Set password",
                "SET PASSWORD FOR '" + socname.replace('-', '_') + "'@'%%' = %s", password
            )

        self.log("Send new password")
        mail_users(self.society, "MySQL database password reset", "mysql-password", password=password, requester=self.owner)

    def __repr__(self): return "<ResetMySQLSocietyPassword {0.society_society}>".format(self)
    def __str__(self): return "Reset society MySQL password: {0.society_society}".format(self)


@add_job
class CreatePostgresUserDatabase(Job):
    JOB_TYPE = 'create_postgres_user_database'

    def __init__(self, row):
        self.row = row

    @classmethod
    def new(cls, member):
        require_approval = member.danger
        return cls.create(member, {}, require_approval)

    def run(self, sess):
        crsid = self.owner.crsid
        assert crsid.isalnum()

        usercreated = False
        password = make_pwd()
        dbcreated = False

        with pgsql_context(self) as (db, cursor):
            sql_exec(self, cursor, "Check for existing user", "SELECT rolname FROM pg_roles WHERE rolname = %s", crsid)
            results = cursor.fetchall()

            if len(results) == 0:
                sql_exec(self, cursor, "Create user", "CREATE USER " + crsid + " ENCRYPTED PASSWORD %s NOCREATEDB NOCREATEUSER", password)
                usercreated = True
            else:
                sql_exec(self, cursor, "(Re-)enable user logins", "ALTER ROLE " + crsid + " LOGIN")

            sql_exec(self, cursor, "Check for existing database", "SELECT datname FROM pg_database WHERE datname = %s", crsid)
            results = cursor.fetchall()

            if len(results) == 0:
                # CREATE DATABASE not supported inside a transaction
                cursor.execute("COMMIT")
                sql_exec(self, cursor, "Create database", "CREATE DATABASE " + crsid + " OWNER " + crsid)
                cursor.execute("BEGIN")
                dbcreated = True

            if not dbcreated and not usercreated:
                raise JobFailed(crsid + " already has a functioning database")

        self.log("Send new password")
        mail_users(self.owner, "PostgreSQL database created", "postgres-create", password=password)

    def __repr__(self): return "<CreatePostgresUserDatabase {0.owner_crsid}>".format(self)
    def __str__(self): return "Create user PostgreSQL database: {0.owner.crsid} ({0.owner.name})".format(self)


@add_job
class ResetPostgresUserPassword(Job):
    JOB_TYPE = 'reset_postgres_user_password'

    # NB: also used to create a Postgres user (in cases where a database doesn't need to be created)

    def __init__(self, row):
        self.row = row

    @classmethod
    def new(cls, member):
        require_approval = member.danger
        return cls.create(member, {}, require_approval)

    def run(self, sess):
        crsid = self.owner.crsid
        assert crsid.isalnum()

        password = make_pwd()

        with pgsql_context(self) as (db, cursor):
            sql_exec(self, cursor, "Check for existing user", "SELECT rolname FROM pg_roles WHERE rolname = %s", crsid)
            results = cursor.fetchall()

            if len(results) == 0:
                sql_exec(self, cursor, "Create user", "CREATE USER " + crsid + " ENCRYPTED PASSWORD %s NOCREATEDB NOCREATEUSER", password)
            else:
                sql_exec(self, cursor, "(Re-)enable user logins", "ALTER ROLE " + crsid + " LOGIN")
                sql_exec(self, cursor, "Reset password", "ALTER USER " + crsid + " PASSWORD %s", password)

        self.log("Send new password")
        mail_users(self.owner, "PostgreSQL database password reset", "postgres-password", password=password)

    def __repr__(self): return "<ResetPostgresUserPassword {0.owner_crsid}>".format(self)
    def __str__(self): return "Reset user PostgreSQL password: {0.owner.crsid} ({0.owner.name})".format(self)


@add_job
class CreatePostgresSocietyDatabase(SocietyJob):
    JOB_TYPE = 'create_postgres_society_database'

    def __init__(self, row):
        self.row = row

    @classmethod
    def new(cls, member, society):
        args = {"society": society.society}
        require_approval = society.danger or member.danger
        return cls.create(member, args, require_approval)

    def run(self, sess):
        crsid = self.owner.crsid
        assert crsid.isalnum()
        socname = self.society_society
        assert utils.is_valid_socname(socname)

        usercreated = False
        userpassword = make_pwd()
        socusercreated = False
        socpassword = make_pwd()
        dbcreated = False

        with pgsql_context(self) as (db, cursor):
            sql_exec(self, cursor, "Check for existing owner user", "SELECT usename FROM pg_shadow WHERE usename = %s", crsid)
            results = cursor.fetchall()

            if len(results) == 0:
                sql_exec(self, cursor, "Create owner user", "CREATE USER " + crsid + " ENCRYPTED PASSWORD %s NOCREATEDB NOCREATEUSER", userpassword)
                usercreated = True
            else:
                sql_exec(self, cursor, "(Re-)enable owner user logins", "ALTER ROLE " + crsid + " LOGIN")

            sql_exec(self, cursor, "Check for existing society user", "SELECT usename FROM pg_shadow WHERE usename = '" + socname + "'")
            results = cursor.fetchall()

            if len(results) == 0:
                sql_exec(self, cursor, "Create society user", "CREATE USER " + socname + " ENCRYPTED PASSWORD %s NOCREATEDB NOCREATEUSER", socpassword)
                socusercreated = True
            else:
                sql_exec(self, cursor, "(Re-)enable society user logins", "ALTER ROLE " + socname + " LOGIN")

            sql_exec(self, cursor, "Check for existing society database", "SELECT datname FROM pg_database WHERE datname = %s", socname)
            results = cursor.fetchall()

            if len(results) == 0:
                # CREATE DATABASE not supported inside a transaction
                cursor.execute("COMMIT")
                sql_exec(self, cursor, "Create society database", "CREATE DATABASE " + socname + " OWNER " + socname)
                cursor.execute("BEGIN")
                dbcreated = True

            self.log("Grant owner access")
            sql_exec(self, cursor, "Grant owner access", "GRANT " + socname + " TO " + crsid)

            if not dbcreated and not usercreated and not socusercreated:
                raise JobFailed(socname + " already has a functioning database")

        self.log("Send society password")
        mail_users(self.society, "PostgreSQL database created", "postgres-create", password=socpassword, requester=self.owner)
        if usercreated:
            self.log("Send owner password")
            mail_users(self.owner, "PostgreSQL account created", "postgres-account", password=userpassword, database=self.society)

    def __repr__(self): return "<CreatePostgresSocietyDatabase {0.society_society}>".format(self)
    def __str__(self): return "Create society PostgreSQL database: {0.society_society}".format(self)


@add_job
class ResetPostgresSocietyPassword(SocietyJob):
    JOB_TYPE = 'reset_postgres_society_password'

    def __init__(self, row):
        self.row = row

    @classmethod
    def new(cls, member, society):
        args = {"society": society.society}
        require_approval = society.danger or member.danger
        return cls.create(member, args, require_approval)

    def run(self, sess):
        crsid = self.owner.crsid
        assert crsid.isalnum()
        socname = self.society_society
        assert utils.is_valid_socname(socname)

        password = make_pwd()

        with pgsql_context(self) as (db, cursor):
            sql_exec(self, cursor, "Check for existing user", "SELECT usename FROM pg_shadow WHERE usename = %s", socname)
            results = cursor.fetchall()

            if len(results) == 0:
                raise JobFailed(socname + " does not have a Postgres user")

            sql_exec(self, cursor, "Reset password", "ALTER USER " + socname + " PASSWORD %s", password)

        self.log("Send new password")
        mail_users(self.society, "PostgreSQL database password reset", "postgres-password", password=password, requester=self.owner)

    def __repr__(self): return "<ResetPostgresSocietyPassword {0.society_society}>".format(self)
    def __str__(self): return "Reset society PostgreSQL password: {0.society_society}".format(self)


@add_job
class AddSocietyVhost(SocietyJob):
    JOB_TYPE = 'add_society_vhost'

    def __init__(self, row):
        self.row = row
        self.domain_text = render_domain_text(self.domain)

    @classmethod
    def new(cls, member, society, domain, root):
        root = "public_html/{}".format(root) if root else None
        args = {"society": society.society, "domain": domain, "root": root}
        # TODO: We don't validate whether a user is allowed a given vhost, which extends to
        # subdomains of other users' domains / wildcard, other srcf.net domains etc.
        require_approval = True  # society.danger or member.danger
        return cls.create(member, args, require_approval)

    domain = property(lambda s: s.row.args["domain"])
    root = property(lambda s: s.row.args["root"])

    def __repr__(self): return "<AddSocietyVhost {0.society_society} {0.domain}>".format(self)
    def __str__(self): return "Add custom society domain: {0.society_society} ({0.domain} -> {0.root})".format(self)

    def run(self, sess):
        self.log("Add domain entry")
        sess.add(Domain(class_="soc",
                        owner=self.society_society,
                        domain=self.domain,
                        root=self.root,
                        wild=False))

        self.log("Send confirmation")
        mail_users(self.society, "Custom domain added", "add-vhost", domain=self.domain_text, root=self.root)


@add_job
class ChangeSocietyVhostDocroot(SocietyJob):
    JOB_TYPE = 'change_society_vhost_docroot'

    def __init__(self, row):
        self.row = row
        self.domain_text = render_domain_text(self.domain)

    @classmethod
    def new(cls, member, society, domain, root):
        root = "public_html/{}".format(root) if root else None
        args = {"society": society.society, "domain": domain, "root": root}
        require_approval = society.danger or member.danger
        return cls.create(member, args, require_approval)

    domain = property(lambda s: s.row.args["domain"])
    root = property(lambda s: s.row.args["root"])

    def __repr__(self): return "<ChangeSocietyVhostDocroot {0.society_society} {0.domain}>".format(self)
    def __str__(self): return "Change custom society domain root: {0.society_society} ({0.domain_text} -> {0.root})".format(self)

    def run(self, sess):
        self.log("Change domain entry")
        results = sess.query(Domain).filter(Domain.class_ == "soc",
                                            Domain.owner == self.society_society,
                                            Domain.domain == self.domain).all()

        if not results:
            raise JobFailed("{0.domain} does not exist", self)
        elif len(results) > 1:
            raise JobFailed("Multiple entries for {0.domain}", self)
        domain = results[0]

        domain.root = self.root
        sess.add(domain)

        self.log("Send confirmation")
        mail_users(self.society, "Custom domain document root changed", "change-vhost-docroot", domain=self.domain_text, root=self.root)


@add_job
class RemoveSocietyVhost(SocietyJob):
    JOB_TYPE = 'remove_society_vhost'

    def __init__(self, row):
        self.row = row
        self.domain_text = render_domain_text(self.domain)

    @classmethod
    def new(cls, member, society, domain):
        args = {"society": society.society, "domain": domain}
        require_approval = society.danger or member.danger
        return cls.create(member, args, require_approval)

    domain = property(lambda s: s.row.args["domain"])

    def __repr__(self): return "<RemoveSocietyVhost {0.society_society} {0.domain}>".format(self)
    def __str__(self): return "Remove custom society domain: {0.society_society} ({0.domain})".format(self)

    def run(self, sess):
        self.log("Lookup domain entry")
        try:
            domain = sess.query(Domain).filter(Domain.domain == self.domain)[0]
        except IndexError:
            raise JobFailed("{0.domain} does not exist".format(self))
        if not domain.owner == self.society_society:
            raise JobFailed("{0.domain} is not owned by {0.society_society}".format(self))

        self.log("Remove domain entry")
        sess.delete(domain)

        self.log("Send confirmation")
        mail_users(self.society, "Custom domain removed", "remove-vhost", domain=self.domain_text)
