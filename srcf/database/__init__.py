import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


__all__ = ["MailHandler", "Member", "Society",
           "LogLevel", "Domain", "HTTPSCert", "JobState", "Job", "JobLog",
           "RESTRICTED", "assert_readwrite", "Session", "url"]

from .schema import RESTRICTED, POSTGRES_USER
from .schema import MailHandler, Member, Society
from .schema import LogLevel, Domain, HTTPSCert, JobState, Job, JobLog


class RestrictedAccess(RuntimeError):
    def __init__(self):
        super(RestrictedAccess, self).__init__(
            "Don't have write access to the membership database")


def assert_readwrite():
    if RESTRICTED:
        raise RestrictedAccess


# try and use a privileged user if we can, otherwise read only
url = "postgresql://{user}@postgres/sysadmins".format(user=POSTGRES_USER)
engine = create_engine(url)
Session = sessionmaker(bind=engine)
