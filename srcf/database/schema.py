from __future__ import print_function, unicode_literals

from binascii import unhexlify
import os
import pwd

import six

from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, Enum, Numeric
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import HSTORE
from sqlalchemy.schema import Table, FetchedValue, CheckConstraint, ForeignKey, DDL
from sqlalchemy.orm import relationship, backref
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.hybrid import hybrid_property

from .compat import MemberCompat, SocietyCompat, AdminsSetCompat


__all__ = ["Member", "Society", "PendingAdmin",
           "POSTGRES_USER", "RESTRICTED"]


# Should we make the notes & danger flags, and pending-admins
# tables available?

# These postgres roles have special permissions / are mentioned
# in the schema. Everyone else should connect as 'nobody'
schema_users = ("root", "srcf-admin", "hades")

# When connecting over a unix socket, postgres uses `getpeereid`
# for authentication; this is the number that matters:
euid_name = pwd.getpwuid(os.geteuid()).pw_name
if euid_name in schema_users or euid_name.endswith("-adm"):
    POSTGRES_USER = euid_name
else:
    POSTGRES_USER = "nobody"
is_root = POSTGRES_USER == "root" or POSTGRES_USER.endswith("-adm")
is_webapp = POSTGRES_USER == "srcf-admin"
is_hades = POSTGRES_USER == "hades"

RESTRICTED = not is_root


VALID_MAIL_HANDLERS = ('forward', 'pip', 'hades')


def _hexdump(raw):
    rendered = "".join(chr(x) if len(repr(chr(x))) == 3 else "." for x in range(256))
    safe = []
    for pos in range(0, len(raw), 16):
        line = raw[pos:pos + 16]
        hex_ = " ".join("{:02x}".format(c) for c in line)
        if len(line) > 8:
            hex_ = "{} {}".format(hex_[:24], hex_[24:])
        chars = "".join(rendered[c] if c < len(rendered) else "." for c in line)
        safe.append("{:08x}  {:48s}  |{}|".format(pos, hex_, chars))
    return "\n".join(safe)


CRSID_TYPE = String(7)
SOCIETY_TYPE = String(16)

Base = declarative_base()


class Member(Base, MemberCompat):
    __tablename__ = 'members'

    crsid = Column(CRSID_TYPE, CheckConstraint('crsid = lower(crsid)'),
                   primary_key=True)
    surname = Column(String(100))
    preferred_name = Column(String(100))
    member = Column(Boolean, nullable=False)
    user = Column(Boolean, nullable=False)
    disk_quota_gb = Column(Integer, FetchedValue())
    disk_usage_gb = Column(Numeric, FetchedValue())
    disk_usage_updated = Column(DateTime(timezone=True), FetchedValue())
    if is_root or is_webapp:
        uid = Column(Integer, FetchedValue())
        gid = Column(Integer, FetchedValue())
        email = Column(String(100), CheckConstraint("email ~ E'@'"), unique=True)
        # FetchedValue: these columns are set by triggers (see below)
        joined = Column(DateTime(timezone=True), FetchedValue())
        modified = Column(DateTime(timezone=True), FetchedValue())
        danger = Column(Boolean, nullable=False, server_default='f')
        notes = Column(Text, nullable=False, server_default='')
        domains = relationship("Domain", primaryjoin="foreign(Domain.owner) == Member.crsid")
    if is_root or is_webapp or is_hades:
        # Beware: Enum doesn't validate until sqlalchemy 1.1
        mail_handler = Column(Enum(*VALID_MAIL_HANDLERS), nullable=False, server_default='pip')

    __table_args__ = (
        CheckConstraint("""
            (NOT member OR (surname IS NOT NULL AND
                            preferred_name IS NOT NULL AND
                            email IS NOT NULL AND
                            joined IS NOT NULL))
        """, name="members_must_have_details"),
        CheckConstraint('member OR NOT "user"', name="users_must_be_members"),
    )

    def __str__(self):
        return self.crsid

    def __repr__(self):
        if is_root or is_webapp:
            m = ' member' if self.member else ' ex-member'
            u = ' user' if self.user else ''
            flags = m + u
            r = '<Member {0} {1} {2}{3}>'.format(self.crsid, self.name, self.email, flags)
        else:
            r = '<Member {0} {1}>'.format(self.crsid, self.name)
        if not six.PY3:
            r = r.encode("utf8")
        return r

    def __eq__(self, other):
        if not isinstance(other, Member):
            return False
        else:
            return self.crsid == other.crsid

    def __hash__(self):
        return hash(self.crsid)

    @hybrid_property
    def name(self):
        """Joins :attr:`preferred_name` and :attr:`surname`"""
        return self.preferred_name + " " + self.surname


society_admins = Table(
    'society_admins', Base.metadata,
    Column('crsid', CRSID_TYPE,
           ForeignKey('members.crsid'), primary_key=True),
    Column('society', SOCIETY_TYPE,
           ForeignKey('societies.society'), primary_key=True),
)


class Society(Base, SocietyCompat):
    __tablename__ = "societies"

    society = Column(SOCIETY_TYPE, CheckConstraint('society = lower(society)'),
                     primary_key=True)
    description = Column(String(100), nullable=False)
    disk_quota_gb = Column(Integer, FetchedValue())
    disk_usage_gb = Column(Numeric, FetchedValue())
    disk_usage_updated = Column(DateTime(timezone=True), FetchedValue())
    if is_root or is_webapp:
        uid = Column(Integer, FetchedValue())
        gid = Column(Integer, FetchedValue())
        joined = Column(DateTime(timezone=True), FetchedValue())
        modified = Column(DateTime(timezone=True), FetchedValue())
        role_email = Column(String(100), CheckConstraint("email ~ E'@'"))
        danger = Column(Boolean, nullable=False, server_default='f')
        notes = Column(Text, nullable=False, server_default='')

    admins = relationship("Member",
                          secondary=society_admins, collection_class=AdminsSetCompat,
                          backref=backref("societies", collection_class=set))

    if is_root or is_webapp:
        pending_admins = relationship("PendingAdmin", backref=backref("society"))
        domains = relationship("Domain", primaryjoin="foreign(Domain.owner) == Society.society")

    def __str__(self):
        return self.society

    def __repr__(self):
        orphaned = '' if self.admins else ' orphaned'
        return '<Society {0}{1}>'.format(self.society, orphaned)

    def __eq__(self, other):
        if not isinstance(other, Society):
            return False
        else:
            return self.society == other.society

    def __hash__(self):
        return hash(self.society)

    def __contains__(self, other):
        if isinstance(other, Member):
            return other in self.admins
        elif isinstance(other, six.string_types):
            return other in self.admin_crsids
        else:
            return False

    @property
    def admin_crsids(self):
        """:attr:`admins`, as a set of strings (crsids)"""
        return frozenset(m.crsid for m in self.admins)

    @hybrid_property
    def email(self):
        """society-admins@srcf.net address"""
        return self.society + "-admins@srcf.net"


if is_root or is_webapp:

    class PendingAdmin(Base):
        __tablename__ = "pending_society_admins"

        # There is no ForeignKey constraint here because this table exists to
        # reference users that don't exist yet.
        crsid = Column(CRSID_TYPE, CheckConstraint('crsid = lower(crsid)'),
                       primary_key=True)
        society_society = Column(SOCIETY_TYPE,
                                 ForeignKey('societies.society'),
                                 name="society",
                                 primary_key=True)

        def __str__(self):
            return "{0} {1}".format(self.crsid, self.society.society)

        def __repr__(self):
            return '<PendingAdmin {0} {1}>'.format(self.crsid, self.society.society)

    class Domain(Base):
        __tablename__ = "domains"
        id = Column(Integer, primary_key=True)
        class_ = Column("class", String(7), nullable=False)
        owner = Column(String(16), nullable=False)
        domain = Column(String(256), nullable=False)
        root = Column(String(256))
        wild = Column(Boolean, nullable=False, server_default='f')
        danger = Column(Boolean, nullable=False, server_default='f')
        last_good = Column(DateTime(timezone=True))

        def __str__(self):
            return self.domain

        def __repr__(self):
            return "<{}: {} ({} {}){}{}>".format(self.__class__.__name__,
                                                 self.domain,
                                                 self.class_,
                                                 self.owner,
                                                 " @ {}".format(repr(self.root)) if self.root else "",
                                                 " wild" if self.wild else "")

    class HTTPSCert(Base):
        __tablename__ = "https_certs"
        id = Column(Integer, primary_key=True)
        domain = Column(String(256), nullable=False)
        name = Column(String(32))

        def __str__(self):
            return self.domain

        def __repr__(self):
            return "<{}: {} ({})>".format(self.__class__.__name__, self.domain, self.name)

    JobState = Enum('unapproved', 'queued', 'running', 'done', 'failed', 'withdrawn',
                    name='job_state')
    LogType = Enum('started', 'progress', 'output', 'done', 'failed', 'note',
                   name='log_type')
    LogLevel = Enum('debug', 'info', 'warning', 'error', 'critical',
                    name='log_level')

    event.listen(
        Base.metadata,
        "before_create",
        DDL("CREATE EXTENSION hstore")
    )

    class Job(Base):
        __tablename__ = 'jobs'
        job_id = Column(Integer, primary_key=True)
        owner_crsid = Column(CRSID_TYPE, ForeignKey("members.crsid"))
        owner = relationship("Member")
        state = Column(JobState, nullable=False, server_default='unapproved')
        state_message = Column(Text)
        created_at = Column(DateTime)
        type = Column(String(100), nullable=False)
        args = Column(HSTORE, nullable=False)
        environment = Column(Text)

    class JobLog(Base):
        __tablename__ = 'job_log'
        log_id = Column(Integer, primary_key=True)
        job_id = Column(Integer, ForeignKey("jobs.job_id"))
        time = Column(DateTime)
        type = Column(LogType)
        level = Column(LogLevel)
        message = Column(Text)
        raw = Column(Text)

        @property
        def raw_safe(self):
            if not self.raw.startswith("\\x"):
                return self.raw
            raw = unhexlify(self.raw[2:])
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return "[Could not decode output as UTF-8]\n{}".format(_hexdump(raw))

else:

    PendingAdmin = None

    LogLevel = None

    Domain = None
    HTTPSCert = None

    JobState = None
    Job = None
    JobLog = None


def dump_schema():
    from sqlalchemy import create_engine
    import os.path

    directory = os.path.dirname(__file__)
    with open(os.path.join(directory, "triggers.sql")) as f:
        triggers = f.read()
    with open(os.path.join(directory, "grants.sql")) as f:
        grants = f.read()

    event.listen(
        Base.metadata,
        "after_create",
        DDL(triggers)
    )

    event.listen(
        Base.metadata,
        "after_create",
        DDL(grants)
    )

    def dump(sql, *multiparams, **params):
        print(sql.compile(dialect=engine.dialect), ";")
    engine = create_engine('postgresql://', strategy='mock', executor=dump)
    Base.metadata.create_all(engine, checkfirst=False)


if __name__ == "__main__":
    dump_schema()
