import warnings

from .database import Member, queries

__all__ = ["get_members", "get_member", "get_users", "get_user",
           "get_societies", "get_society", "members_and_socs",
           "members", "societies",
           "MemberSet", "SocietySet"]


def _dep(func_name):
    warnings.warn("{0} is deprecated (use srcf.database.queries.*)"
                  .format(func_name), DeprecationWarning)


def get_members(crsids=None):
    if crsids is None:
        _dep("get_members")
        return queries.list_members()
    else:
        warnings.warn("get_members(crsids) is deprecated", DeprecationWarning)
        return queries._sess().query(Member).filter(Member.crsid.in_(crsids))


def get_member(crsid):
    _dep("get_member")
    return queries.get_member(crsid)


def get_users(crsids=None):
    if crsids is None:
        _dep("get_users")
        return queries.list_users()
    else:
        warnings.warn("get_members(crsids...) is deprecated",
                      DeprecationWarning)
        return (queries._sess().query(Member)
                       .filter(Member.crsid.in_(crsids))
                       .filter(Member.user))


def get_user(crsid):
    _dep("get_user")
    return queries.get_user(crsid)


def get_societies(name=None, admin=None):
    if name is not None:
        warnings.warn("get_societies(name=...) is deprecated",
                      DeprecationWarning)
        soc = get_society(name)

        if admin is not None:
            warnings.warn("get_societies(admin=...) is deprecated",
                          DeprecationWarning)
            if admin in soc.admin_crsids:
                return [soc]
            else:
                return []
        else:
            return [soc]
    elif admin is not None:
        return queries.get_member(admin).societies
    else:
        _dep("get_societies")
        return queries.list_societies()


def get_society(name):
    _dep("get_society")
    return queries.get_society(name)


def members_and_socs():
    _dep("members_and_socs")
    return queries.dict_members(), queries.dict_societies()


def members():
    _dep("members")
    return queries.dict_members()


def societies():
    _dep("societies")
    return queries.dict_societies()


MemberSet = SocietySet = set
