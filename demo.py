"""Run the minimal two-agent experiment in a temporary, isolated database."""
from pathlib import Path
import tempfile

from seed import Conflict, Store


def main():
    temporary_root = Path(__file__).resolve().parent / "artifacts" / "tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
        store = Store.create(Path(directory) / "demo.db")
        initial = store.propose(store.head(), {
            "api.py": b"def old(): return 1\n",
            "a.py": b"A0\n", "b.py": b"B0\n"}, message="initial files")
        base = store.land(initial)["revision"]

        a = store.propose(base, {"a.py": b"A1\n"}, author="agent A")
        b = store.propose(base, {"b.py": b"B1\n"}, author="agent B")
        print("1. Two independent proposals from the same base:")
        print("   A:", store.land(a)["status"])
        print("   B, after HEAD advanced:", store.land(b)["status"])
        assert store.read("a.py") == b"A1\n" and store.read("b.py") == b"B1\n"

        competing = store.propose(base, {"a.py": b"A2\n", "extra.py": b"extra\n"})
        print("2. A stale edit of the same file:")
        try:
            store.land(competing)
        except Conflict as exc:
            print("   refused:", ", ".join(exc.differences))
        else:
            raise AssertionError("same-file proposal must conflict")
        assert store.read("extra.py") is None
        print("   extra.py was not partially published")

        caller = store.propose(base, {"caller.py": b"answer = old()\n"}, reads=["api.py"])
        api = store.propose(base, {"api.py": b"def new(): return 1\n"})
        store.land(api)
        print("3. Different write path, but a declared read dependency changed:")
        try:
            store.land(caller)
        except Conflict as exc:
            print("   refused:", ", ".join(exc.differences))
        else:
            raise AssertionError("declared read condition must conflict")

        print("4. The same dependency, left undeclared:")
        blind = store.propose(base, {"caller.py": b"answer = old()\n"})
        print("   proposal:", store.land(blind)["status"])
        try:
            exec(store.read("api.py") + store.read("caller.py"), {})
        except NameError:
            print("   actual program: NameError (semantic correctness still needs tests)")
        else:
            raise AssertionError("this intentional broken program must fail")

        print("5. Retry an already landed proposal:")
        head = store.head()
        print("   A:", store.land(a)["status"])
        assert store.head() == head
        print("   no additional revision; all demo assertions passed")


if __name__ == "__main__":
    main()
