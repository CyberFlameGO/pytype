"""Base support for generating classes from data declarations.

Contains common functionality used by dataclasses, attrs and namedtuples.
"""

import abc
import collections
import logging

from pytype import abstract
from pytype import abstract_utils
from pytype import class_mixin
from pytype import overlay_utils
from pytype import special_builtins


log = logging.getLogger(__name__)


# type aliases for convenience
Param = overlay_utils.Param
Attribute = class_mixin.Attribute
AttributeKinds = class_mixin.AttributeKinds


class Ordering:
  """Possible orderings for get_class_locals."""
  # Order by each variable's first annotation. For example, for
  #   class Foo:
  #     x: int
  #     y: str
  #     x: float
  # the locals will be [(x, Instance(float)), (y, Instance(str))]. Note that
  # unannotated variables will be skipped, and the values of later annotations
  # take precedence over earlier ones.
  FIRST_ANNOTATE = object()
  # Order by each variable's last definition. So for
  #   class Foo:
  #     x = 0
  #     y = 'hello'
  #     x = 4.2
  # the locals will be [(y, Instance(str)), (x, Instance(float))]. Note that
  # variables without assignments will be skipped.
  LAST_ASSIGN = object()


class Decorator(abstract.PyTDFunction, metaclass=abc.ABCMeta):
  """Base class for decorators that generate classes from data declarations."""

  # Defaults for the args that we support (dataclasses only support 'init',
  # but the others default to false so they should not affect anything).
  _DEFAULT_ARGS = {
      "init": True,
      "kw_only": False,
      "auto_attribs": False,
  }

  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    # Decorator.call() is invoked first with args, then with the class to
    # decorate, so we need to first store the args and then associate them to
    # the right class.
    self._current_args = None
    self.args = {}  # map from each class we decorate to its args

  @abc.abstractmethod
  def decorate(self, node, cls):
    """Apply the decorator to cls."""

  def update_kwargs(self, args):
    self._current_args = self._DEFAULT_ARGS.copy()
    for k, v in args.namedargs.items():
      if k in self._current_args:
        try:
          self._current_args[k] = abstract_utils.get_atomic_python_constant(v)
        except abstract_utils.ConversionError:
          self.vm.errorlog.not_supported_yet(
              self.vm.frames, "Non-constant argument to decorator: %r" % k)

  def init_name(self, attr):
    """Attribute name as an __init__ keyword, could differ from attr.name."""
    return attr.name

  def make_init(self, node, cls, attrs):
    pos_params = []
    kwonly_params = []
    all_kwonly = self.args[cls]["kw_only"]
    for attr in attrs:
      if not attr.init:
        continue
      typ = attr.init_type or attr.typ
      # call self.init_name in case the name differs from the field name -
      # e.g. attrs removes leading underscores from attrib names when
      # generating kwargs for __init__.
      param = Param(
          name=self.init_name(attr),
          typ=typ,
          default=attr.default)

      # The kw_only arg is ignored in python2; using it is not an error.
      # kw_only=False in a field does not override kw_only=True in the class.
      if self.vm.PY3 and (all_kwonly or attr.kw_only):
        kwonly_params.append(param)
      else:
        pos_params.append(param)

    return overlay_utils.make_method(
        self.vm, node, "__init__", pos_params, kwonly_params)

  def call(self, node, func, args):
    """Construct a decorator, and call it on the class."""
    args = args.simplify(node, self.vm)
    self.match_args(node, args)

    # There are two ways to use a decorator:
    #   @decorator(...)
    #   class Foo: ...
    # or
    #   @decorator
    #   class Foo: ...
    # In the first case, call() is invoked twice: once with kwargs to create the
    # decorator object and once with the decorated class as a posarg. So we call
    # update_kwargs on the first invocation, setting _current_args, and skip it
    # on the second.
    # In the second case, we call update_kwargs on the first and only
    # invocation. (Although namedargs is empty in this case, bool(namedargs) is
    # True as long as namedargs is an abstract.Dict object.)
    if args.namedargs and not self._current_args:
      self.update_kwargs(args)

    # NOTE: @dataclass is py3-only and has explicitly kwonly args in its
    # constructor.
    #
    # @attr.s does not take positional arguments in typical usage, but
    # technically this works:
    #   class Foo:
    #     x = attr.ib()
    #   Foo = attr.s(Foo, **kwargs)
    #
    # Unfortunately, it also works to pass kwargs as posargs; we will at least
    # reject posargs if the first arg is not a Callable.
    if not args.posargs:
      return node, self.to_variable(node)

    cls_var = args.posargs[0]
    # We should only have a single binding here
    cls, = cls_var.data

    if not isinstance(cls, class_mixin.Class):
      # There are other valid types like abstract.Unsolvable that we don't need
      # to do anything with.
      return node, cls_var

    self.args[cls] = self._current_args
    # Reset _current_args so we don't use old args for a new class.
    self._current_args = None

    # decorate() modifies the cls object in place
    self.decorate(node, cls)
    return node, cls_var


class FieldConstructor(abstract.PyTDFunction):
  """Implements constructors for fields."""

  def get_kwarg(self, args, name, default):
    if name not in args.namedargs:
      return default
    try:
      return abstract_utils.get_atomic_python_constant(args.namedargs[name])
    except abstract_utils.ConversionError:
      self.vm.errorlog.not_supported_yet(
          self.vm.frames, "Non-constant argument %r" % name)


def is_method(var):
  if var is None:
    return False
  return isinstance(var.data[0], (
      abstract.INTERPRETER_FUNCTION_TYPES,
      special_builtins.ClassMethodInstance,
      special_builtins.PropertyInstance,
      special_builtins.StaticMethodInstance
  ))


def is_dunder(name):
  return name.startswith("__") and name.endswith("__")


def add_member(node, cls, name, typ):
  if typ.formal:
    # If typ contains a type parameter, we mark it as empty so that instances
    # will use __annotations__ to fill in concrete type parameter values.
    instance = typ.vm.convert.empty.to_variable(node)
  else:
    # See test_attr.TestAttrib.test_repeated_default - keying on the name
    # prevents attributes from sharing the same default object.
    _, instance = typ.vm.init_class(node, typ, extra_key=name)
  cls.members[name] = instance


def get_class_locals(cls_name, allow_methods, ordering, vm):
  """Gets a dictionary of the class's local variables.

  Args:
    cls_name: The name of an abstract.InterpreterClass.
    allow_methods: A bool, whether to allow methods as variables.
    ordering: A classgen.Ordering describing the order in which the variables
      should appear.
    vm: The VirtualMachine.

  Returns:
    A collections.OrderedDict of the locals.
  """
  # TODO(rechen): Once we drop Python 2 support, either use a normal dict or
  # replace key deletion with OrderedDict.move_to_end().
  out = collections.OrderedDict()
  if cls_name not in vm.local_ops:
    # See TestAttribPy3.test_cannot_decorate in tests/py3/test_attr.py. The
    # class will not be in local_ops if a previous decorator hides it.
    return out
  for op in vm.local_ops[cls_name]:
    if is_dunder(op.name):
      continue
    local = vm.annotated_locals[cls_name][op.name]
    if not allow_methods and is_method(local.orig):
      continue
    if ordering is Ordering.FIRST_ANNOTATE:
      if not op.is_annotate() or op.name in out:
        continue
    else:
      assert ordering is Ordering.LAST_ASSIGN
      if not op.is_assign():
        continue
      elif op.name in out:
        del out[op.name]
    out[op.name] = local
  return out


def make_replace_method(vm, node, cls, *, kwargs_name="kwargs"):
  """Create a replace() method for a dataclass."""
  # This is used by several packages that extend dataclass.
  # The signature is
  #   def replace(self: T, **kwargs) -> T
  typevar = abstract.TypeParameter(abstract_utils.T + cls.name, vm, bound=cls)
  return overlay_utils.make_method(
      vm=vm,
      node=node,
      name="replace",
      return_type=typevar,
      self_param=overlay_utils.Param("self", typevar),
      kwargs=overlay_utils.Param(kwargs_name),
  )


def get_or_create_annotations_dict(members, vm):
  """Get __annotations__ from members map, create and attach it if not present.

  The returned dict is also referenced by members, so it is safe to mutate.

  Args:
    members: A dict of member name to variable.
    vm: TypegraphVirtualMachine instance.

  Returns:
    members['__annotations__'] unpacked as a python dict
  """
  annotations_dict = abstract_utils.get_annotations_dict(members)
  if annotations_dict is None:
    annotations_dict = abstract.AnnotationsDict({}, vm)
    members["__annotations__"] = annotations_dict.to_variable(vm.root_node)
  return annotations_dict
