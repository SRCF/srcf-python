import os
import re
from ldap3 import Server, Connection, ALL, ALL_ATTRIBUTES
import stat
import shutil
import configparser
import pymysql


__all__ = ["email_re", "ldapsearch", "is_admin", "mysql_conn"]


# yeah whatever.
email_re = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z][A-Za-z]+$")


# LDAP helper
def ldapsearch(crsid):
    server = Server('ldap.lookup.cam.ac.uk', get_info=ALL)
    conn = Connection(server, auto_bind=True)
    conn.search('ou=people, o=University of Cambridge,dc=cam,dc=ac,dc=uk',
                '(uid={0})'.format(crsid), attributes=ALL_ATTRIBUTES)
    r = conn.entries

    if len(r) != 1:
        raise KeyError(crsid)
    return r[0]


def is_admin(member):
    if member is None:
        return False
    for soc in member.societies:
        if soc.society == "srcf-admin":
            return True
    return False


mysql_passwd = None


def mysql_conn():
    global mysql_passwd
    if not mysql_passwd:
        my_cnf = configparser.ConfigParser()
        my_cnf.read("/societies/srcf-admin/.my.cnf")
        mysql_passwd = my_cnf.get('client', 'password')
    conn = pymysql.connect(user="srcf_admin", db="srcf_admin", passwd=mysql_passwd)
    conn.autocommit = True
    return conn


def is_valid_socname(s):
    return re.match(r'^[a-z0-9_-]+$', s)


def nfs_aware_chown(path, *args, **kwargs):
    # NFSv4 is fickle.  The protocol might either use numeric UIDs/GIDs, or
    # (canonically, but not by default) user/group names.  The latter requires
    # the NFS server to know about the user/group in advance.  This might go
    # wrong if we only just created them, especially since NetApp caches
    # nonexistence for a long time.
    #
    # We *try* not to do anything that will cause nonexistence to be cached
    # (we update NIS and wait a good while for the server before we chown)
    # but actions elsewhere (e.g. someone trying to manually chown to a
    # not-yet-existent user) might have already triggered the problem.  We
    # can't do anything about that except give the poor sysadmin a hint.
    try:
        os.chown(path, *args, **kwargs)
    except OSError as e:
        if e.errno == 22:  # EINVAL
            dev = os.stat(path).st_dev
            dev_str = "%s:%s" % (os.major(dev), os.minor(dev))
            with open("/proc/net/nfsfs/volumes", "r") as f:
                for line in f:
                    fields = line.split()
                    if fields[3] == dev_str:
                        server = fields[1]
                        ver = fields[0]
                        with open("/proc/net/nfsfs/servers", "r") as ff:
                            for lline in ff:
                                ffields = lline.split()
                                if ffields[1] == server:
                                    hostname = ffields[4]
                                    raise Exception("Got EINVAL when attempting to chown(%s) on %s via NFS%s.  "
                                                    "That might mean that the user or group is unknown to the NFS server.  "
                                                    "If this seems wrong, it may have cached nonexistence.  "
                                                    "If it's a NetApp, try 'nfs nsdb flush' on %s, or "
                                                    "just wait an hour or two then retry." % (path, hostname, ver, hostname))
        raise


# Copy a tree, overriding the owner, group and permissions
# (for installing /etc/skel into homedirs)
# "Inspired by" (an antique version of) shutil.copytree but:
#  -  Errors are immediately fatal
#  -  The destination directory must already exist
# /!\ User permissions are copied to group permissions
def copytree_chown_chmod(src, dst, uid, gid):
    for name in os.listdir(src):
        srcname = os.path.join(src, name)
        dstname = os.path.join(dst, name)
        linkto = None
        if os.path.islink(srcname):
            linkto = os.readlink(srcname)
            os.symlink(linkto, dstname)
        elif os.path.isdir(srcname):
            os.mkdir(dstname)
            copytree_chown_chmod(srcname, dstname, uid, gid)
        else:
            shutil.copy(srcname, dstname)
        # The rest is "inspired by" shutil.copystat...
        # (but doesn't handle xattrs or flags because we don't need that)
        os.chown(dstname, uid, gid, follow_symlinks=False)
        st = os.stat(srcname, follow_symlinks=False)
        os.utime(dstname, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
        if linkto is None:
            mode = stat.S_IMODE(st.st_mode)
            # Copy user mode bits to group mode
            mode = (mode & 0o7707) | ((mode & 0o0700) >> 3)
            os.chmod(dstname, mode)
