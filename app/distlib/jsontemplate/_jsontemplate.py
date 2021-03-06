# Copyright (C) 2009 Andy Chu
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# $Id: _jsontemplate.py 240 2009-05-11 20:26:56Z martijn.faassen $

"""Python implementation of json-template.

JSON Template is a minimal and powerful templating language for transforming a
JSON dictionary to arbitrary text.

To use this module, you will typically use the Template constructor, and catch
various exceptions thrown.  You may also want to use the FromFile/FromString
methods, which allow Template constructor options to be embedded in the template
string itself.

Other functions are exposed for tools which may want to process templates.
"""

__author__ = 'Andy Chu'

__all__ = ['Error', 'CompilationError', 'EvaluationError',
           'BadFormatter', 'MissingFormatter', 'ConfigurationError',
           'TemplateSyntaxError', 'UndefinedVariable',
           'CompileTemplate', 'FromString', 'FromFile',
           'Template', 'expand']

import cStringIO
import pprint
import re
import sys

# For formatters
import cgi  # cgi.escape
import urllib  # for urllib.encode


class Error(Exception):
  """Base class for all exceptions in this module.

  Thus you can "except jsontemplate.Error: to catch all exceptions thrown by
  this module.
  """

  def __str__(self):
    """This helps people debug their templates.

    If a variable isn't defined, then some context is shown in the traceback.
    TODO: Attach context for other errors.
    """
    if hasattr(self, 'near'):
      return '%s\n\nNear: %s' % (self.args[0], pprint.pformat(self.near))
    else:
      return self.args[0]


class CompilationError(Error):
  """Base class for errors that happen during the compilation stage."""


class EvaluationError(Error):
  """Base class for errors that happen when expanding the template.

  This class of errors generally involve the data dictionary or the execution of
  the formatters.
  """
  def __init__(self, msg, original_exception=None):
    Error.__init__(self, msg)
    self.original_exception = original_exception


class BadFormatter(CompilationError):
  """A bad formatter was specified, e.g. {variable|BAD}"""

class MissingFormatter(CompilationError):
  """
  Raised when formatters are required, and a variable is missing a formatter.
  """

class ConfigurationError(CompilationError):
  """
  Raised when the Template options are invalid and it can't even be compiled.
  """

class TemplateSyntaxError(CompilationError):
  """Syntax error in the template text."""

class UndefinedVariable(EvaluationError):
  """The template contains a variable not defined by the data dictionary."""


_SECTION_RE = re.compile(r'(repeated)?\s*section\s+(\S+)')


class _ProgramBuilder(object):
  """
  Receives method calls from the parser, and constructs a tree of _Section()
  instances.
  """

  def __init__(self, more_formatters):
    """
    Args:
      more_formatters: A function which returns a function to apply to the
          value, given a format string.  It can return None, in which case the
          _DEFAULT_FORMATTERS dictionary is consulted.
    """
    self.current_block = _Section()
    self.stack = [self.current_block]
    self.more_formatters = more_formatters

  def Append(self, statement):
    """
    Args:
      statement: Append a literal
    """
    self.current_block.Append(statement)

  def _GetFormatter(self, format_str):
    """
    The user's formatters are consulted first, then the default formatters.
    """
    formatter = (
        self.more_formatters(format_str) or _DEFAULT_FORMATTERS.get(format_str))

    if formatter:
      return formatter
    else:
      raise BadFormatter('%r is not a valid formatter' % format_str)

  def AppendSubstitution(self, name, formatters):
    formatters = [self._GetFormatter(f) for f in formatters]
    self.current_block.Append((_DoSubstitute, (name, formatters)))

  def NewSection(self, repeated, section_name):
    """For sections or repeated sections."""

    new_block = _Section(section_name)
    if repeated:
      func = _DoRepeatedSection
    else:
      func = _DoSection

    self.current_block.Append((func, new_block))
    self.stack.append(new_block)
    self.current_block = new_block

  def NewClause(self, name):
    # TODO: Raise errors if the clause isn't appropriate for the current block
    # isn't a 'repeated section' (e.g. alternates with in a non-repeated
    # section)
    self.current_block.NewClause(name)

  def EndSection(self):
    self.stack.pop()
    self.current_block = self.stack[-1]

  def Root(self):
    # It's assumed that we call this at the end of the program
    return self.current_block


class _Section(object):
  """Represents a (repeated) section."""

  def __init__(self, section_name=None):
    """
    Args:
      section_name: name given as an argument to the section
    """
    self.section_name = section_name

    # Pairs of func, args, or a literal string
    self.current_clause = []
    self.statements = {'default': self.current_clause}

  def __repr__(self):
    return '<Block %s>' % self.section_name

  def Statements(self, clause='default'):
    return self.statements.get(clause, [])

  def NewClause(self, clause_name):
    new_clause = []
    self.statements[clause_name] = new_clause
    self.current_clause = new_clause

  def Append(self, statement):
    """Append a statement to this block."""
    self.current_clause.append(statement)


class _ScopedContext(object):
  """Allows scoped lookup of variables.

  If the variable isn't in the current context, then we search up the stack.
  """

  def __init__(self, context, undefined_str):
    """
    Args:
      context: The root context
      undefined_str: See Template() constructor.
    """
    self.stack = [context]
    self.undefined_str = undefined_str

  def PushSection(self, name):
    new_context = self.stack[-1].get(name)
    self.stack.append(new_context)
    return new_context

  def Pop(self):
    self.stack.pop()

  def CursorValue(self):
    return self.stack[-1]

  def __iter__(self):
    """Assumes that the top of the stack is a list."""

    # The top of the stack is always the current context.
    self.stack.append(None)
    for item in self.stack[-2]:
      self.stack[-1] = item
      yield item
    self.stack.pop()

  def _Undefined(self, name):
    if self.undefined_str is None:
      raise UndefinedVariable('%r is not defined' % name)
    else:
      return self.undefined_str

  def _LookUpStack(self, name):
    """Look up the stack for the given name."""
    i = len(self.stack) - 1
    while 1:
      context = self.stack[i]

      if not hasattr(context, 'get'):  # Can't look up names in a list or atom
        i -= 1
      else:
        value = context.get(name)
        if value is None:  # A key of None or a missing key are treated the same
          i -= 1
        else:
          return value

      if i <= -1:  # Couldn't find it anywhere
        return self._Undefined(name)

  def Lookup(self, name):
    """Get the value associated with a name in the current context.

    The current context could be an dictionary in a list, or a dictionary
    outside a list.

    Args:
      name: name to lookup, e.g. 'foo' or 'foo.bar.baz'

    Returns:
      The value, or self.undefined_str

    Raises:
      UndefinedVariable if self.undefined_str is not set
    """
    parts = name.split('.')
    value = self._LookUpStack(parts[0])

    # Now do simple lookups of the rest of the parts
    for part in parts[1:]:
      try:
        value = value[part]
      except (KeyError, TypeError):  # TypeError for non-dictionaries
        return self._Undefined(part)

    return value


def _ToString(x):
  if type(x) in (str, unicode):
    return x
  else:
    return pprint.pformat(x)


def _HtmlAttrValue(x):
  return cgi.escape(x, quote=True)


# See http://google-ctemplate.googlecode.com/svn/trunk/doc/howto.html for more
# escape types.
#
# Also, we might want to take a look at Django filters.
#
# This is a *public* constant, so that callers can use it construct their own
# formatter lookup dictionaries, and pass them in to Template.
_DEFAULT_FORMATTERS = {
    'html': cgi.escape,

    # The 'htmltag' name is deprecated.  The html-attr-value name is preferred
    # because it can be read with "as":
    #   {url|html-attr-value} means:
    #   "substitute 'url' as an HTML attribute value"
    'html-attr-value': _HtmlAttrValue,
    'htmltag': _HtmlAttrValue,

    'raw': lambda x: x,
    # Used for the length of a list.  Can be used for the size of a dictionary
    # too, though I haven't run into that use case.
    'size': lambda value: str(len(value)),

    # The argument is a dictionary, and we get a a=1&b=2 string back.
    'url-params': urllib.urlencode,  

    # The argument is an atom, and it takes 'Search query?' -> 'Search+query%3F'
    'url-param-value': urllib.quote_plus,  # param is an atom

    # The default formatter, when no other default is specifier.  For debugging,
    # this could be lambda x: json.dumps(x, indent=2), but here we want to be
    # compatible to Python 2.4.
    'str': _ToString,

    # Just show a plain URL on an HTML page (without anchor text).
    'plain-url': lambda x: '<a href="%s">%s</a>' % (
        cgi.escape(x, quote=True), cgi.escape(x)),

    # Placeholders for "standard names".  We're not including them by default
    # since they require additional dependencies.  We can provide a part of the
    # "lookup chain" in formatters.py for people people want the dependency.
    
    # 'json' formats arbitrary data dictionary nodes as JSON strings.  'json'
    # and 'js-string' are identical (since a JavaScript string *is* JSON).  The
    # latter is meant to be serve as extra documentation when you want a string
    # argument only, which is a common case.  
    'json': None,
    'js-string': None,
    }


def SplitMeta(meta):
  """Split and validate metacharacters.

  Example: '{}' -> ('{', '}')

  This is public so the syntax highlighter and other tools can use it.
  """
  n = len(meta)
  if n % 2 == 1:
    raise ConfigurationError(
        '%r has an odd number of metacharacters' % meta)
  return meta[:n/2], meta[n/2:]


_token_re_cache = {}

def MakeTokenRegex(meta_left, meta_right):
  """Return a (compiled) regular expression for tokenization.

  Args:
    meta_left, meta_right: e.g. '{' and '}'

  - The regular expressions are memoized.
  - This function is public so the syntax highlighter can use it.
  """
  key = meta_left, meta_right
  if key not in _token_re_cache:
    # - Need () grouping for re.split
    # - For simplicity, we allow all characters except newlines inside
    #   metacharacters ({} / [])
    _token_re_cache[key] = re.compile(
        r'(' +
        re.escape(meta_left) +
        r'.+?' +
        re.escape(meta_right) +
        r')')
  return _token_re_cache[key]


# Examples:

( LITERAL_TOKEN,  # "Hi"
  SUBSTITUTION_TOKEN,  # {var|html}
  SECTION_TOKEN,  # {.section name}
  REPEATED_SECTION_TOKEN,  # {.repeated section name}
  CLAUSE_TOKEN,  # {.or}
  END_TOKEN,  # {.end}
  ) = range(6)


def _MatchDirective(token):
  """Helper function for matching certain directives."""

  if token in ('.or', '.alternates with'):
    return CLAUSE_TOKEN, token[1:]

  if token == '.end':
    return END_TOKEN, None

  match = _SECTION_RE.match(token[1:])
  if match:
    repeated, section_name = match.groups()
    if repeated:
      return REPEATED_SECTION_TOKEN, section_name
    else:
      return SECTION_TOKEN, section_name

  return None, None  # no match


def _Tokenize(template_str, meta_left, meta_right):
  """Yields tokens, which are 2-tuples (TOKEN_TYPE, token_string)."""

  trimlen = len(meta_left)

  token_re = MakeTokenRegex(meta_left, meta_right)

  for line in template_str.splitlines(True):  # retain newlines
    tokens = token_re.split(line)

    # Check for a special case first.  If a comment or "block" directive is on a
    # line by itself (with only space surrounding it), then the space is
    # omitted.  For simplicity, we don't handle the case where we have 2
    # directives, say '{.end} # {#comment}' on a line.

    if len(tokens) == 3:
      # ''.isspace() == False, so work around that
      if (tokens[0].isspace() or not tokens[0]) and \
         (tokens[2].isspace() or not tokens[2]):
        token = tokens[1][trimlen : -trimlen]

        if token.startswith('#'):
          continue  # The whole line is omitted

        token_type, token = _MatchDirective(token)
        if token_type is not None:
          yield token_type, token  # Only yield the token, not space
          continue

    # The line isn't special; process it normally.

    for i, token in enumerate(tokens):
      if i % 2 == 0:
        yield LITERAL_TOKEN, token

      else:  # It's a "directive" in metachracters

        assert token.startswith(meta_left), repr(token)
        assert token.endswith(meta_right), repr(token)
        token = token[trimlen : -trimlen]

        # It's a comment
        if token.startswith('#'):
          continue

        if token.startswith('.'):

          literal = {
              '.meta-left': meta_left,
              '.meta-right': meta_right,
              '.space': ' ',
              '.tab': '\t',
              '.newline': '\n',
              }.get(token)

          if literal is not None:
            yield LITERAL_TOKEN, literal
            continue

          token_type, token = _MatchDirective(token)
          if token_type is not None:
            yield token_type, token

        else:  # Now we know the directive is a substitution.
          yield SUBSTITUTION_TOKEN, token


def CompileTemplate(
    template_str, builder=None, meta='{}', format_char='|',
    more_formatters=lambda x: None, default_formatter='str'):
  """Compile the template string, calling methods on the 'program builder'.

  Args:
    template_str: The template string.  It should not have any compilation
        options in the header -- those are parsed by FromString/FromFile
    builder: Something with the interface of _ProgramBuilder
    meta: The metacharacters to use
    more_formatters: A function which maps format strings to
        *other functions*.  The resulting functions should take a data
        dictionary value (a JSON atom, or a dictionary itself), and return a
        string to be shown on the page.  These are often used for HTML escaping,
        etc.  There is a default set of formatters available if more_formatters
        is not passed.
    default_formatter: The formatter to use for substitutions that are missing a
        formatter.  The 'str' formatter the "default default" -- it just tries
        to convert the context value to a string in some unspecified manner.

  Returns:
    The compiled program (obtained from the builder)

  Raises:
    The various subclasses of CompilationError.  For example, if
    default_formatter=None, and a variable is missing a formatter, then
    MissingFormatter is raised.

  This function is public so it can be used by other tools, e.g. a syntax
  checking tool run before submitting a template to source control.
  """
  builder = builder or _ProgramBuilder(more_formatters)
  meta_left, meta_right = SplitMeta(meta)

  # : is meant to look like Python 3000 formatting {foo:.3f}.  According to
  # PEP 3101, that's also what .NET uses.
  # | is more readable, but, more importantly, reminiscent of pipes, which is
  # useful for multiple formatters, e.g. {name|js-string|html}
  if format_char not in (':', '|'):
    raise ConfigurationError(
        'Only format characters : and | are accepted (got %r)' % format_char)

  # If we go to -1, then we got too many {end}.  If end at 1, then we're missing
  # an {end}.
  balance_counter = 0

  for token_type, token in _Tokenize(template_str, meta_left, meta_right):

    if token_type == LITERAL_TOKEN:
      if token:
        builder.Append(token)
      continue

    if token_type == REPEATED_SECTION_TOKEN:
      builder.NewSection(True, token)
      balance_counter += 1
      continue

    if token_type == SECTION_TOKEN:
      builder.NewSection(False, token)
      balance_counter += 1
      continue

    if token_type == CLAUSE_TOKEN:
      builder.NewClause(token)
      continue

    if token_type == END_TOKEN:
      balance_counter -= 1
      if balance_counter < 0:
        # TODO: Show some context for errors
        raise TemplateSyntaxError(
            'Got too many %send%s statements.  You may have mistyped an '
            "earlier 'section' or 'repeated section' directive."
            % (meta_left, meta_right))
      builder.EndSection()
      continue

    if token_type == SUBSTITUTION_TOKEN:
      parts = token.split(format_char)
      if len(parts) == 1:
        if default_formatter is None:
          raise MissingFormatter('This template requires explicit formatters.')
        # If no formatter is specified, the default is the 'str' formatter,
        # which the user can define however they desire.
        name = token
        formatters = [default_formatter]
      else:
        name = parts[0]
        formatters = parts[1:]

      builder.AppendSubstitution(name, formatters)

  if balance_counter != 0:
    raise TemplateSyntaxError('Got too few %send%s statements' %
        (meta_left, meta_right))

  return builder.Root()


_OPTION_RE = re.compile(r'^([a-zA-Z\-]+):\s*(.*)')
# TODO: whitespace mode, etc.
_OPTION_NAMES = ['meta', 'format-char', 'default-formatter', 'undefined-str']


def FromString(s, more_formatters=lambda x: None, _constructor=None):
  """Like FromFile, but takes a string."""

  f = cStringIO.StringIO(s)
  return FromFile(f, more_formatters=more_formatters, _constructor=_constructor)


def FromFile(f, more_formatters=lambda x: None, _constructor=None):
  """Parse a template from a file, using a simple file format.

  This is useful when you want to include template options in a data file,
  rather than in the source code.

  The format is similar to HTTP or E-mail headers.  The first lines of the file
  can specify template options, such as the metacharacters to use.  One blank
  line must separate the options from the template body.

  Example:

    default-formatter: none
    meta: {{}}
    format-char: :
    <blank line required>
    Template goes here: {{variable:html}}

  Args:
    f: A file handle to read from.  Caller is responsible for opening and
    closing it.
  """
  _constructor = _constructor or Template

  options = {}

  # Parse lines until the first one that doesn't look like an option
  while 1:
    line = f.readline()
    match = _OPTION_RE.match(line)
    if match:
      name, value = match.group(1), match.group(2)

      # Accept something like 'Default-Formatter: raw'.  This syntax is like
      # HTTP/E-mail headers.
      name = name.lower()

      if name in _OPTION_NAMES:
        name = name.replace('-', '_')
        value = value.strip()
        if name == 'default_formatter' and value.lower() == 'none':
          value = None
        options[name] = value
      else:
        break
    else:
      break

  if options:
    if line.strip():
      raise CompilationError(
          'Must be one blank line between template options and body (got %r)'
          % line)
    body = f.read()
  else:
    # There were no options, so no blank line is necessary.
    body = line + f.read()

  return _constructor(body, more_formatters=more_formatters, **options)


class Template(object):
  """Represents a compiled template.

  Like many template systems, the template string is compiled into a program,
  and then it can be expanded any number of times.  For example, in a web app,
  you can compile the templates once at server startup, and use the expand()
  method at request handling time.  expand() uses the compiled representation.

  There are various options for controlling parsing -- see CompileTemplate.
  Don't go crazy with metacharacters.  {}, [], {{}} or <> should cover nearly
  any circumstance, e.g. generating HTML, CSS XML, JavaScript, C programs, text
  files, etc.
  """

  def __init__(self, template_str, builder=None, undefined_str=None,
               **compile_options):
    """
    Args:
      template_str: The template string.
      undefined_str: A string to appear in the output when a variable to be
          substituted is missing.  If None, UndefinedVariable is raised.
          (Note: This is not really a compilation option, because affects
          template expansion rather than compilation.  Nonetheless we make it a
          constructor argument rather than an .expand() argument for
          simplicity.)

    It also accepts all the compile options that CompileTemplate does.
    """
    self._program = CompileTemplate(
        template_str, builder=builder, **compile_options)
    self.undefined_str = undefined_str

  #
  # Public API
  #

  def render(self, data_dict, callback):
    """Low level method to expands the template piece by piece.

    Args:
      data_dict: The JSON data dictionary.
      callback: A callback which should be called with each expanded token.

    Example: You can pass 'f.write' as the callback to write directly to a file
    handle.
    """
    context = _ScopedContext(data_dict, self.undefined_str)
    _Execute(self._program.Statements(), context, callback)

  def expand(self, *args, **kwargs):
    """Expands the template with the given data dictionary, returning a string.

    This is a small wrapper around render(), and is the most convenient
    interface.

    Args:
      The JSON data dictionary.  Like the builtin dict() constructor, it can
      take a single dictionary as a positional argument, or arbitrary keyword
      arguments.

    Returns:
      The return value could be a str() or unicode() instance, depending on the
      the type of the template string passed in, and what the types the strings
      in the dictionary are.
    """
    if args:
      if len(args) == 1:
        data_dict = args[0]
      else:
        raise TypeError(
            'expand() only takes 1 positional argument (got %s)' % args)
    else:
      data_dict = kwargs

    tokens = []
    self.render(data_dict, tokens.append)
    return ''.join(tokens)

  def tokenstream(self, data_dict):
    """Yields a list of tokens resulting from expansion.

    This may be useful for WSGI apps.  NOTE: In the current implementation, the
    entire expanded template must be stored memory.

    NOTE: This is a generator, but JavaScript doesn't have generators.
    """
    tokens = []
    self.render(data_dict, tokens.append)
    for token in tokens:
      yield token


def _DoRepeatedSection(args, context, callback):
  """{repeated section foo}"""

  block = args

  if block.section_name == '@':
    # If the name is @, we stay in the enclosing context, but assume it's a
    # list, and repeat this block many times.
    items = context.CursorValue()
    if type(items) is not list:
      raise EvaluationError('Expected a list; got %s' % type(items))
    pushed = False
  else:
    items = context.PushSection(block.section_name)
    pushed = True

  # TODO: what if items is a dictionary?

  if items:
    last_index = len(items) - 1
    statements = block.Statements()
    alt_statements = block.Statements('alternates with')
    # NOTE: Iteration mutates the context!
    for i, _ in enumerate(context):
      # Execute the statements in the block for every item in the list.  Execute
      # the alternate block on every iteration except the last.
      # Each item could be an atom (string, integer, etc.) or a dictionary.
      _Execute(statements, context, callback)
      if i != last_index:
        _Execute(alt_statements, context, callback)

  else:
    _Execute(block.Statements('or'), context, callback)

  if pushed:
    context.Pop()


def _DoSection(args, context, callback):
  """{section foo}"""

  block = args
  # If a section isn't present in the dictionary, or is None, then don't show it
  # at all.
  if context.PushSection(block.section_name):
    _Execute(block.Statements(), context, callback)
    context.Pop()
  else:  # Empty list, None, False, etc.
    context.Pop()
    _Execute(block.Statements('or'), context, callback)


def _DoSubstitute(args, context, callback):
  """Variable substitution, e.g. {foo}"""

  name, formatters = args

  # So we can have {.section is_new}new since {@}{.end}.  Hopefully this idiom
  # is OK.
  if name == '@':
    value = context.CursorValue()
  else:
    try:
      value = context.Lookup(name)
    except TypeError, e:
      raise EvaluationError(
          'Error evaluating %r in context %r: %r' % (name, context, e))

  for f in formatters:
    try:
      value = f(value)
    except KeyboardInterrupt:
      raise
    except Exception, e:
      raise EvaluationError(
          'Formatting value %r with formatter %s raised exception: %r' %
          (value, formatters, e), original_exception=e)

  # TODO: Require a string/unicode instance here?
  if value is None:
    raise EvaluationError('Evaluating %r gave None value' % name)
  callback(value)


def _Execute(statements, context, callback):
  """Execute a bunch of template statements in a ScopedContext.

  Args:
    callback: Strings are "written" to this callback function.

  This is called in a mutually recursive fashion.
  """

  for i, statement in enumerate(statements):
    if isinstance(statement, basestring):
      callback(statement)
    else:
      # In the case of a substitution, args is a pair (name, formatter).
      # In the case of a section, it's a _Section instance.
      try:
        func, args = statement
        func(args, context, callback)
      except UndefinedVariable, e:
        # Show context for statements
        start = max(0, i-3)
        end = i+3
        e.near = statements[start:end]
        raise


def expand(template_str, dictionary, **kwargs):
  """Free function to expands a template string with a data dictionary.

  This is useful for cases where you don't care about saving the result of
  compilation (similar to re.match('.*', s) vs DOT_STAR.match(s))
  """
  t = Template(template_str, **kwargs)
  return t.expand(dictionary)
