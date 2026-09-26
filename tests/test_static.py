"""Static checks the unit tests cannot make.

main.py wires everything together inside nested closures that only run
against a live gateway, so a name used there but never imported passes
every unit test, imports cleanly, and fails only in production -- at
the moment the code path is first taken. 2.0.70 shipped exactly that:
back_to_back was called in the hourly imbalance check but never
imported, so every FVG crashed the check and no imbalance alert fired.

This walks every module's symbol table and fails on any name that is
read as a global but never defined, imported or built in. Stdlib only.
"""

import builtins
import pathlib
import symtable
import unittest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "crackedalert"

ALLOWED = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__spec__", "__loader__",
    "__package__", "__builtins__",
}


def undefined_names(source: str, path: str) -> set:
    """Names read as globals anywhere in the module but never bound at
    module level (assigned, imported, def or class) and not builtins."""
    top = symtable.symtable(source, path, "exec")
    defined = {sym.get_name() for sym in top.get_symbols()
               if sym.is_assigned() or sym.is_imported()
               or sym.is_namespace()}
    missing = set()

    def walk(table):
        for sym in table.get_symbols():
            if sym.is_referenced() and sym.is_global():
                name = sym.get_name()
                if name not in defined and name not in ALLOWED:
                    missing.add(name)
        for child in table.get_children():
            walk(child)

    walk(top)
    return missing


class UndefinedNameTests(unittest.TestCase):

    def test_no_module_uses_a_name_it_never_defines_or_imports(self):
        modules = sorted(SRC.rglob("*.py"))
        self.assertTrue(modules, "found no modules under %s" % SRC)
        problems = {}
        for path in modules:
            missing = undefined_names(path.read_text(encoding="utf-8"),
                                      str(path))
            if missing:
                problems[str(path.relative_to(SRC))] = sorted(missing)
        self.assertEqual(problems, {})

    def test_the_check_catches_a_missing_import_in_a_nested_closure(self):
        # The exact shape of the 2.0.70 bug. If this stops failing the
        # checker, the guard above has gone blind.
        source = (
            "from .fvg import fresh_imbalance\n"
            "async def main():\n"
            "    async def verdict(bars):\n"
            "        which = fresh_imbalance(bars)\n"
            "        return which is not None and back_to_back(bars)\n"
        )
        self.assertEqual(undefined_names(source, "<test>"), {"back_to_back"})

    def test_the_check_accepts_the_fixed_version(self):
        source = (
            "from .fvg import back_to_back, fresh_imbalance\n"
            "async def main():\n"
            "    async def verdict(bars):\n"
            "        which = fresh_imbalance(bars)\n"
            "        return which is not None and back_to_back(bars)\n"
        )
        self.assertEqual(undefined_names(source, "<test>"), set())

    def test_closure_variables_are_not_mistaken_for_globals(self):
        # Names from an enclosing function scope are free variables, not
        # globals, and must not be flagged.
        source = (
            "def outer():\n"
            "    gate = object()\n"
            "    def inner():\n"
            "        return gate\n"
            "    return inner\n"
        )
        self.assertEqual(undefined_names(source, "<test>"), set())


if __name__ == "__main__":
    unittest.main()
