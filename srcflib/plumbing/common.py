"""
Shared helper methods and base classes.
"""

from enum import Enum
from functools import total_ordering, wraps
import inspect
import logging
import os
import platform
import subprocess
from typing import Any, Callable, Generator, Generic, Iterable, List, Optional, Set, TypeVar, Union

from sqlalchemy.orm import Session as SQLASession

from srcf import pwgen
from srcf.database import Member, Society


LOG = logging.getLogger(__name__)

T = TypeVar("T")

Owner = Union[Member, Society]
"""
Union type combining `Member` and `Society` database objects, useful for functions that work with
either account type.
"""

Collect = Generator["Result[Any]", None, T]
"""
Generic type for the return value of functions using `Result.collect`.
"""


class Unset:
    """
    Constructor of generic default values for optional but nullable parameters.
    """

    def __bool__(self):
        return False

    def __repr__(self):
        return "UNSET"


UNSET = Unset()
"""
Global generic default value.
"""


def owner_name(owner: Union[Owner, str]) -> str:
    """
    Return a `Member` CRSid, or a `Society` short name.
    """
    if isinstance(owner, str):
        return owner
    elif isinstance(owner, Member):
        return owner.crsid
    elif isinstance(owner, Society):
        return owner.society
    else:
        raise TypeError(owner)


def owner_desc(owner: Owner, admins: bool = False) -> str:
    """
    Return a `Member` full name, or a `Society` description optionally addressing its admins.
    """
    if isinstance(owner, Member):
        return owner.name
    elif isinstance(owner, Society):
        if admins:
            return "{} admins".format(owner.description)
        else:
            return owner.description
    else:
        raise TypeError(owner)


def owner_home(owner: Owner, public: bool = False) -> str:
    """
    Return a `Member` or `Society` home directory path, either private or public.
    """
    if isinstance(owner, Member):
        path = os.path.join("/home", owner.crsid)
    elif isinstance(owner, Society):
        path = os.path.join("/societies", owner.society)
    else:
        raise TypeError(owner)
    if public:
        path = "/public{}".format(path)
    return path


def owner_website(owner: Owner):
    """
    Return a member or society's default website address.
    """
    if isinstance(owner, Member):
        key = "user"
    elif isinstance(owner, Society):
        key = "soc"
    else:
        raise TypeError(owner)
    return "https://{}.{}.srcf.net".format(owner_name(owner), key)


def get_members(sess: SQLASession, *crsids: str) -> Set[Member]:
    """
    Fetch multiple `Member` objects by their CRSids.
    """
    users = sess.query(Member).filter(Member.crsid.in_(crsids)).all()
    missing = set(crsids) - {user.crsid for user in users}
    if missing:
        raise KeyError("Missing members: {}".format(", ".join(sorted(missing))))
    else:
        return set(users)


@total_ordering
class State(Enum):
    """
    Enumeration used by `Result` to declare whether the action happened.
    """

    unchanged = 0
    """
    No action required, the request and current state are consistent.
    """
    success = 1
    """
    The action was completed without issues.
    """
    created = 2
    """
    The action resulted in the creation of a new object or record.
    """

    def __bool__(self):
        return bool(self.value)

    def __lt__(self, other: "State"):
        return self.value < other.value if isinstance(other, State) else NotImplemented


class Result(Generic[T]):
    """
    State and optional accompanying value from a unit of work.

    For a simple plumbing action, just create a new result directly with the resulting `State` and
    a value if relevant:

        def unit():
            # Create a database record, call an external command etc.
            return Result(State.success, True)

    For a task that combines multiple results, see `Result.collect`.  The state of such a result is
    based on all of its parts -- if any changes were made, the outer result also reports a change.

    A result can be checked for truthiness, which is `False` if no changes were made.

    A result can also be converted to a string, which produces a tree-like summary of changes:

        module:task success True
            module:unit1 unchanged
            module:unit2 success
    """

    @classmethod
    def collect_value(cls, fn: Callable[..., Collect[T]]) -> Callable[..., "Result[T]"]:
        """
        Decorator: build a `Result` from multiple sub-tasks:

            def plumb_b() -> Result[str]: ...

            @Result.collect
            def task() -> Collect[str]:
                yield plumb_a()
                result = yield from plumb_b()
                if result:
                    yield plumb_c()
                return result.value

        The inner function this decorator wraps should be a generator of `Result` objects.

        The return value of the wrapper function will be a new `Result` object, whose `parts` will
        be those collected sub-task results, and whose `value` will be set to the return value of
        the inner function (i.e. the example above will return a `Result[str]`).
        """
        @wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> Result[T]:
            state = None
            value = UNSET
            parts: List[Result[Any]] = []
            gen = fn(*args, **kwargs)
            try:
                while True:
                    result = next(gen)
                    parts.append(result)
            except StopIteration as ex:
                value = ex.value
            return cls(state, value, parts, fn)
        return inner

    @classmethod
    def collect(cls, fn: Callable[..., Collect[None]]) -> Callable[..., "Result[Unset]"]:
        """
        Decorator: variant of `Result.collect_value` with no return value parsing, producing
        results with unset values.

        Note that the inner function's return type should be `Collect[None]`, not `Collect[Unset]`.
        """
        @wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> "Result[Unset]":
            return Result(parts=fn(*args, **kwargs), caller=fn)
        return inner

    def __init__(self, state: Optional[State] = None, value: Union[T, Unset] = UNSET,
                 parts: Iterable["Result[Any]"] = (), caller: Optional[Callable[..., Any]] = None):
        self._state = state
        self._value = value
        self.parts = tuple(parts)
        self.caller = "<unknown>"
        # Inspection magic to log the calling method, e.g. `module.sub:Class.method`.
        name = None
        if not caller:
            frame = inspect.currentframe()
            try:
                name = frame.f_back.f_code.co_name
                caller = frame.f_back.f_globals[name]
            except (AttributeError, KeyError):
                pass
        if caller:
            self.caller = "{}:{}".format(caller.__module__, caller.__qualname__)
        elif name:
            self.caller = name

    @property
    def state(self) -> State:
        """
        Modification state of the unit of work.

        This may be set directly, computed from `parts`, or defaulted to `State.unchanged`.
        """
        if self._state:
            return self._state
        elif self.parts:
            return max(result.state for result in self.parts)
        else:
            return State.unchanged

    @state.setter
    def state(self, state: State) -> None:
        self._state = state

    @property
    def value(self) -> T:
        """
        Return value produced by the unit of work.

        Accessing this attribute will raise `ValueError` if no value has been set.
        """
        if isinstance(self._value, Unset):
            raise ValueError("No value set")
        return self._value

    @value.setter
    def value(self, value: T) -> None:
        self._value = value

    def __bool__(self) -> bool:
        return bool(self.state)

    def __iter__(self) -> Generator["Result[T]", None, "Result[T]"]:
        # Syntactic sugar used by `yield from` expressions in `Result.collect()`.
        yield self
        return self

    def __repr__(self) -> str:
        params = [str(self.state)]
        if not isinstance(self._value, Unset):
            params.append(repr(self._value))
        if self.parts:
            params.append("<{} parts>".format(len(self.parts)))
        return "{}({})".format(self.__class__.__name__, ", ".join(params))

    def __str__(self) -> str:
        tree = "{}: {}".format(self.caller, self.state.name)
        if not isinstance(self._value, Unset):
            tree = "{} {!r}".format(tree, self._value)
        if self.parts:
            for result in self.parts:
                tree += "\n    {}".format(str(result).replace("\n", "\n    "))
        return tree


class Password:
    """
    Container of randomly generated passwords.  Use `str(passwd)` to get the actual value.
    """

    def __init__(self, value: str, template: str = "{}"):
        self._value = value
        self._template = template

    def __str__(self):
        return self._template.format(self._value)

    def __repr__(self):
        return "<{}: {!r}>".format(self.__class__.__name__, self._template.format("***"))

    @classmethod
    def new(cls) -> "Password":
        """
        Generate a fresh new password.
        """
        return cls(pwgen().decode("utf-8"))

    def wrap(self, template: str) -> "Password":
        """
        Embed a plaintext password into a larger string, and wrap that as a `Password`:

            >>> passwd = Password("secret")
            >>> line = passwd.wrap("username:{}")
            >>> line
            <Password: 'username:***'>
            >>> str(line)
            'username:secret'
        """
        return self.__class__(self._value, template.format(self._template))


def require_host(*hosts: str) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Only allow a function to be called on the given hosts, identified by hostname:

        @require_hosts(Hosts.USER)
        def create_user(username): ...
    """
    def outer(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def inner(*args: Any, **kwargs: Any):
            host = platform.node()
            if host not in hosts:
                raise RuntimeError("{}() can't be used on host {}, requires {}"
                                   .format(fn.__name__, host, "/".join(hosts)))
            return fn(*args, **kwargs)
        return inner
    return outer


def command(args: List[str], input_: Optional[Union[str, Password]] = None,
            output: bool = False) -> "subprocess.CompletedProcess[bytes]":
    """
    Create a subprocess to execute an external command.
    """
    if input_:
        LOG.debug("Exec: %r <<< %r", args, input_)
    else:
        LOG.debug("Exec: %r", args)
    return subprocess.run(args, input=str(input_).encode("utf-8") if input_ else None,
                          stdout=subprocess.PIPE if output else None, check=True)


def make(path: str, *targets: str) -> Result[Unset]:
    """
    Run `make` in a directory.

    The targets are checked to see if they're up-to-date; if they are then `State.unchanged` is
    set in the returned `Result`.
    """
    cmd = ["/usr/bin/make", "-C", path, *targets]
    try:
        command(cmd + ["-q"])
    except subprocess.CalledProcessError:
        pass
    else:
        return Result(State.unchanged)
    command(cmd)
    return Result(State.success)
