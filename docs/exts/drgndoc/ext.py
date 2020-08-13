# Copyright (c) Facebook, Inc. and its affiliates.
# SPDX-License-Identifier: GPL-3.0+

"""
drgn consists of a core C extension and supporting Python code. It also makes
use of type hints. As a result, its documentation generation has a few
requirements:

1. It must work without compiling the C extension, which can't be done on Read
   the Docs because of missing dependencies.
2. It must support generating documentation from type hints (ideally with
   proper markup rather than by including the raw type annotations).
3. It must support type hint stub files.
4. It must support classes/functions/etc. which are defined in one module but
   should canonically be documented in another. This is common for C extensions
   that are wrapped by a higher-level Python module.

The main existing solutions are ruled out by these requirements:

1. sphinx.ext.autodoc (and other solutions based on runtime introspection)
   require excluding the C extension (e.g., with autodoc_mock_imports) and
   providing the documentation for it elsewhere. Additionally, type hints from
   stub files are not available at runtime, so extensions like
   sphinx-autodoc-typehints and sphinx.ext.autodoc.typehints won't work.
2. sphinx.ext.autoapi doesn't generate markup for type hints and doesn't have
   any support for objects which should documented under a different name than
   they were defined. It also only supports documenting directory trees, not
   individual files.

This extension addresses these requirements. In the future, it may be
worthwhile to make it a standalone package, as I imagine other projects that
make heavy use of C extensions have encountered similar issues.

Overall, it works by parsing Python source code and stub files (drgndoc.parse),
building a tree representing the namespace (drgndoc.namespace), and using that
namespace to resolve definitions and type annotations to generate markup
(drgndoc.format).

This also provides a script that can generate docstring definitions from a stub
file for the C extension itself (drgndoc.docstrings).
"""

import docutils.nodes
import docutils.parsers.rst.directives
import docutils.statemachine
import os.path
import re
import sphinx.addnodes
import sphinx.application
import sphinx.environment
import sphinx.util.docutils
import sphinx.util.logging
import sphinx.util.nodes
from typing import Any, Dict, List, cast

from drgndoc.format import Formatter
from drgndoc.namespace import Namespace, ResolvedNode
from drgndoc.parse import (
    Class,
    DocumentedNode,
    Function,
    Import,
    ImportFrom,
    Module,
    Node,
    Variable,
    parse_paths,
)
from drgndoc.util import dot_join


logger = sphinx.util.logging.getLogger(__name__)


# Needed for type checking.
class DrgnDocBuildEnvironment(sphinx.environment.BuildEnvironment):
    drgndoc_namespace: Namespace
    drgndoc_formatter: Formatter


def drgndoc_init(app: sphinx.application.Sphinx) -> None:
    env = cast(DrgnDocBuildEnvironment, app.env)

    paths = [
        os.path.join(app.confdir, path)
        for path in app.config.drgndoc_paths  # type: ignore
    ]
    env.drgndoc_namespace = Namespace(parse_paths(paths, logger.warning))
    env.drgndoc_formatter = Formatter(
        env.drgndoc_namespace,
        [
            (re.compile(pattern), repl)
            for pattern, repl in app.config.drgndoc_substitutions  # type: ignore
        ],
    )


class DrgnDocDirective(sphinx.util.docutils.SphinxDirective):
    env: DrgnDocBuildEnvironment

    required_arguments = 1
    optional_arguments = 0
    option_spec = {
        "include": docutils.parsers.rst.directives.unchanged,
        "exclude": docutils.parsers.rst.directives.unchanged,
    }

    def run(self) -> Any:
        parts = []
        py_module = self.env.ref_context.get("py:module")
        if py_module:
            parts.append(py_module)
        py_classes = self.env.ref_context.get("py:classes", [])
        if py_classes:
            parts.extend(py_classes)
        parts.append(self.arguments[0])
        name = ".".join(parts)
        resolved = self.env.drgndoc_namespace.resolve_global_name(name)
        if not isinstance(resolved, ResolvedNode):
            logger.warning("name %r not found", resolved)
            return []

        docnode = docutils.nodes.section()
        self._run(name, "", resolved, docnode)
        return docnode.children

    def _include_attr(self, attr: ResolvedNode[Node], attr_name: str) -> bool:
        """
        Return whether the given recursive attribute should be documented.

        We recursively include nodes that are:
        1. Not imports.
        2. Match the "include" pattern OR don't start with an underscore.
        AND
        3. Do not match the "exclude" pattern.

        The "include" and "exclude" patterns are applied to the name relative
        to the object being documented by the directive.
        """
        if isinstance(attr.node, (Import, ImportFrom)):
            return False

        if not attr_name:
            return True

        dot = attr_name.rfind(".")
        if dot + 1 < len(attr_name) and attr_name[dot + 1] == "_":
            include_pattern = self.options.get("include")
            if include_pattern is None or not re.fullmatch(include_pattern, attr_name):
                return False
        exclude_pattern = self.options.get("exclude")
        return exclude_pattern is None or not re.fullmatch(exclude_pattern, attr_name)

    def _run(
        self,
        top_name: str,
        attr_name: str,
        resolved: ResolvedNode[Node],
        docnode: docutils.nodes.Node,
    ) -> None:
        if not self._include_attr(resolved, attr_name):
            return
        resolved = cast(ResolvedNode[DocumentedNode], resolved)

        node = resolved.node
        if isinstance(node, Module):
            directive = "py:module"
            return self._run_module(
                top_name, attr_name, cast(ResolvedNode[Module], resolved), docnode
            )

        sourcename = ""
        if resolved.module and resolved.module.node.path:
            sourcename = resolved.module.node.path
        if sourcename:
            self.env.note_dependency(sourcename)

        if isinstance(node, Class):
            directive = "py:class"
        elif isinstance(node, Function):
            directive = "py:method" if resolved.class_ else "py:function"
        elif isinstance(node, Variable):
            directive = "py:attribute" if resolved.class_ else "py:data"
        else:
            assert False, type(node).__name__

        argument = (attr_name or top_name).rpartition(".")[2]
        extra_argument, lines = self.env.drgndoc_formatter.format(
            resolved,
            self.env.ref_context.get("py:module", ""),
            ".".join(self.env.ref_context.get("py:classes", ())),
        )

        contents = docutils.statemachine.StringList()
        contents.append(
            f".. {directive}:: {argument}{extra_argument}", sourcename,
        )
        if isinstance(node, Function):
            if node.async_:
                contents.append("    :async:", sourcename)
            if resolved.class_:
                if node.have_decorator("classmethod") or argument in (
                    "__init_subclass__",
                    "__class_getitem__",
                ):
                    contents.append("    :classmethod:", sourcename)
                if node.have_decorator("staticmethod"):
                    contents.append("    :staticmethod:", sourcename)
        contents.append("", sourcename)
        if lines:
            for line in lines:
                contents.append("    " + line, sourcename)
            contents.append("", sourcename)

        self.state.nested_parse(contents, 0, docnode)
        if isinstance(node, Class):
            for desc in reversed(docnode.children):
                if isinstance(desc, sphinx.addnodes.desc):
                    break
            else:
                logger.warning("desc node not found")
                return
            for desc_content in reversed(desc.children):
                if isinstance(desc_content, sphinx.addnodes.desc_content):
                    break
            else:
                logger.warning("desc_content node not found")
                return

            py_classes = self.env.ref_context.setdefault("py:classes", [])
            py_classes.append(resolved.name)
            self.env.ref_context["py:class"] = resolved.name
            for member in resolved.attrs():
                self._run(
                    top_name, dot_join(attr_name, member.name), member, desc_content
                )
            py_classes.pop()
            self.env.ref_context["py:class"] = py_classes[-1] if py_classes else None

    def _run_module(
        self,
        top_name: str,
        attr_name: str,
        resolved: ResolvedNode[Module],
        docnode: docutils.nodes.Node,
    ) -> None:
        node = resolved.node
        sourcename = node.path or ""
        if sourcename:
            self.env.note_dependency(sourcename)

        contents = docutils.statemachine.StringList()
        if node.docstring:
            for line in node.docstring.splitlines():
                contents.append(line, sourcename)

        sphinx.util.nodes.nested_parse_with_titles(self.state, contents, docnode)

        # If the module docstring defines any sections, then the contents
        # should go inside of the last one.
        section = docnode
        for child in reversed(docnode.children):
            if isinstance(child, docutils.nodes.section):
                section = child
                break

        try:
            old_py_module = self.env.ref_context["py:module"]
            have_old_py_module = True
        except KeyError:
            have_old_py_module = False
        self.env.ref_context["py:module"] = dot_join(top_name, attr_name)
        for attr in resolved.attrs():
            self._run(top_name, dot_join(attr_name, attr.name), attr, section)
        if have_old_py_module:
            self.env.ref_context["py:module"] = old_py_module
        else:
            del self.env.ref_context["py:module"]


def setup(app: sphinx.application.Sphinx) -> Dict[str, Any]:
    app.connect("builder-inited", drgndoc_init)
    # List of modules or packages.
    app.add_config_value("drgndoc_paths", [], "env")
    # List of (regex pattern, substitution) to apply to resolved names.
    app.add_config_value("drgndoc_substitutions", [], "env")
    app.add_directive("drgndoc", DrgnDocDirective)
    return {"env_version": 1, "parallel_read_safe": True, "parallel_write_safe": True}
