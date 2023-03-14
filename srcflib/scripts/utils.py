"""
Helpers for converting methods into scripts, and filling in arguments with database objects.
"""

from functools import wraps
from inspect import cleandoc, signature
import logging
import sys
from typing import Any, Callable, cast, Dict, List, Mapping, Optional, Union

from docopt import docopt
from sqlalchemy.orm import Session as SQLASession

from srcf.database import Member, Session, Society
from srcf.database.queries import get_member, get_member_or_society, get_society
from srcf.mail import SYSADMINS

from ..email import EmailWrapper, Layout, Recipient, SuppressEmails
from ..plumbing.common import Owner


DocOptArgs = Dict[str, Union[bool, str, List[str]]]

NoneType = type(None)


ENTRYPOINTS: List[str] = []


sess = Session(autocommit=True)


def entrypoint(fn: Callable[..., Any]) -> Callable[..., Any]:
    """
    Decorator to make an entrypoint out of a generic function.

    This uses `docopt` to parse arguments according to the method docstring, and will be formatted
    with `{script}` set to the script name.  At minimum, it should contain `Usage: {script}`.

    Functions may optionally accept arguments, but they must be annotated with a recognised type in
    order to be filled in.  The following types are fixed and always available:

    - `DocOptArgs` (a `dict` of input parameters parsed from the usage line)
    - `Session` (a SQLAlchemy session)

    The types `Member`, `Society`, or `Owner` will be used to try and look up a corresponding object
    based on an input parameter matching the variable name (the name must be declared in the usage
    line, either in upper case or surrounded by arrow brackets, e.g. `MEMBER` or `<member>`).

    An example function:

        @entrypoint
        def grant(opts: DocOptArgs, sess: Session, member: Member, society: Society):
            \"""
            Add the member to the society.

            Usage: {script} MEMBER SOCIETY
            \"""
    """
    label = "srcflib-{}-{}".format(fn.__module__.rsplit(".", 1)[-1],
                                   fn.__qualname__).replace("_", "-")

    @wraps(fn)
    def wrap(opts: Optional[DocOptArgs] = None):
        extra: Dict[str, Any] = {}
        script = "{} [--debug] [--suppress-email]".format(label)
        if opts is None:
            doc = cleandoc(fn.__doc__.format(script=script))
            opts = docopt(doc)
        if opts.pop("--debug", False):
            logging.basicConfig(level=logging.DEBUG)
        if opts.pop("--suppress-email", False):
            wrap = SuppressEmails("[{}]".format(label))
        else:
            wrap = ScriptEmailWrapper(label)
        # Detect resolvable-typed arguments and fill in their values.
        sig = signature(fn)
        ok = True
        for param in sig.parameters.values():
            name = param.name
            cls = param.annotation
            if cls is DocOptArgs:
                extra[name] = opts
                continue
            elif cls is SQLASession:
                extra[name] = sess
                continue
            try:
                try:
                    value = cast(str, opts[name.upper()])
                except KeyError:
                    value = cast(str, opts["<{}>".format(name)])
            except KeyError:
                raise RuntimeError("Missing argument {!r}".format(name))
            optional = False
            # Unpick Optional[X] by reading the type object arguments and removing type(None).
            if getattr(cls, "__origin__", None) is Union:
                cls_args = cls.__args__
                if NoneType in cls_args:
                    optional = True
                    # NB. Union[X] for a single type X automatically resolves to X.
                    cls = Union[tuple(arg for arg in cls_args if arg is not NoneType)]
            if value is None and optional:
                extra[name] = None
                continue
            try:
                if cls is Member:
                    extra[name] = get_member(value, sess)
                elif cls is Society:
                    extra[name] = get_society(value, sess)
                elif cls is Owner:
                    extra[name] = get_member_or_society(value, sess)
                else:
                    raise RuntimeError("Bad parameter {!r} type {!r}".format(name, cls))
            except KeyError:
                ok = False
                error("{!r} is not valid for parameter {!r}".format(value, name), colour="1")
        if not ok:
            sys.exit(1)
        try:
            with wrap:
                fn(**extra)
        finally:
            sess.flush()
    wrap.__doc__ = wrap.__doc__.format(script=label)
    # Create a console script line for setup.
    target = "{}:{}".format(fn.__module__, fn.__qualname__)
    ENTRYPOINTS.append("{}={}".format(label, target))
    return wrap


class ScriptEmailWrapper(EmailWrapper):
    """
    Wrapper that uses the script name in email subjects when notifying sysadmins.
    """

    def __init__(self, label: str):
        super().__init__()
        self._label = label

    def render(self, template: str, layout: Layout, target: Optional[Owner], recipient: Recipient,
               extra_context: Optional[Mapping[str, Any]] = None) -> str:
        if recipient == SYSADMINS:
            extra_context = dict(extra_context or (), prefix="[{}]".format(self._label))
        return super().render(template, layout, target, recipient, extra_context)


def confirm(msg: str = "Are you sure?"):
    """
    Prompt for confirmation before destructive actions.
    """
    try:
        yn = input("\033[96m{} [yN]\033[0m ".format(msg))
    except (KeyboardInterrupt, EOFError):
        print()
        yn = "n"
    if yn.lower() not in ("y", "yes"):
        error("Aborted!", exit=1)


def error(msg: Optional[str] = None, *, exit: Optional[int] = None, colour: Optional[str] = None):
    """
    Print an error message and/or exit.
    """
    if msg:
        colour = colour or ("1" if exit else "3")
        print("\033[9{}m{}\033[0m".format(colour, msg), file=sys.stderr)
    if exit is not None:
        sys.exit(exit)
