import sys
from datetime import datetime

from . import Member, Society, Session, assert_readwrite
# direct access to this makes things a lot easier
from .schema import society_admins


MEMBERLIST = "/societies/sysadmins/admin/memberlist"
SOCLIST = "/societies/sysadmins/admin/soclist"
SOCQUEUE = "/societies/srcf/admin/socqueue"


MEMBERLIST_FIELDS = ("crsid", "surname", "firstname", "initials", "email",
                     "status", "joined")
SOCLIST_FIELDS = ("name", "description", "admin_crsids", "joined")


def try_decode(text):
    for encoding in ("ascii", "utf8", "iso8859"):
        try:
            v = text.decode(encoding)
        except UnicodeDecodeError:
            continue
        else:
            if encoding != "ascii":
                print("Decoded", repr(text), "as", encoding, v.strip(), file=sys.stderr)
            return v
    raise UnicodeDecodeError(repr(text))


def read_members():
    with open(MEMBERLIST, 'rb') as f:
        for line in f:
            line = try_decode(line)
            fields = line.strip().split(":")
            user = dict(zip(MEMBERLIST_FIELDS, fields))

            user["preferred_name"] = user["firstname"]
            del user["firstname"]
            user["joined"] = datetime.strptime(user["joined"], "%Y/%m")
            user["member"] = user["status"] not in ("terminated", "revoked")
            user["user"] = user["status"] == "user" or user["crsid"] == "rjd4"
            user["danger"] = user["status"] == "revoked"
            if user["status"] == "honorary":
                user["notes"] = "Honorary Member\n"
            else:
                user["notes"] = ""
            del user["status"]
            del user["initials"]
            yield user


def read_societies(keep_admins=False):
    with open(SOCLIST, 'rb') as f:
        for line in f:
            line = try_decode(line)
            fields = line.strip().split(":")
            soc = dict(zip(SOCLIST_FIELDS, fields))

            soc["society"] = soc["name"]
            soc["joined"] = datetime.strptime(soc["joined"], "%Y/%m")
            soc["danger"] = soc["name"] == "cucc"
            soc["notes"] = ""
            del soc["name"]
            if not keep_admins:
                del soc["admin_crsids"]
            yield soc


def read_society_admins():
    for society in read_societies(keep_admins=True):
        if society["admin_crsids"] != "":
            for crsid in society["admin_crsids"].split(","):
                yield {"society": society["society"], "crsid": crsid}


def read_socqueue():
    with open(SOCQUEUE, 'r') as f:
        for line in f:
            crsid, soc = line.strip().split(":")
            yield crsid, soc


def triggers(session, action):
    assert action in ("ENABLE", "DISABLE")
    for table in ("members", "societies"):
        session.execute("ALTER TABLE {0} {1} TRIGGER {0}_set_joined_trigger"
                        .format(table, action))


def assert_empty(session):
    assert session.query(Member).count() == 0
    assert session.query(Society).count() == 0


def main():
    assert_readwrite()
    session = Session()
    assert_empty(session)
    triggers(session, "DISABLE")

    for member in read_members():
        session.add(Member(**member))
    for society in read_societies():
        session.add(Society(**society))
    session.flush()

    session.execute(society_admins.insert(), list(read_society_admins()))

    session.flush()

    triggers(session, "ENABLE")
    session.commit()


if __name__ == "__main__":
    main()
