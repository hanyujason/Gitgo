import ast

import pytest

from backend.core.update.version import Version
from scripts.ci_version_source import successor_source
from scripts.release_artifacts import ci_successor_product


BASELINE = "1.0.0-alpha.3.1.1.stage4lab.10"


@pytest.mark.parametrize("platform", ["windows", "linux"])
@pytest.mark.parametrize("literal", [repr(BASELINE), '"' + BASELINE + '"'])
def test_real_successor_accepts_either_quote_and_keeps_unrelated_text(platform, literal):
    product = ci_successor_product({"version": BASELINE, "windows_version": "1.0.0.10"}, platform)
    candidate = product["version"]
    prefix = f'"""Keep __version__ = "{BASELINE}" in documentation."""\r\n'
    prefix += f"unrelated = '{BASELINE}'; label = '版本'; "
    before = prefix + "__version__ = " + literal + f"  # keep {BASELINE}\r\n"
    after = successor_source(before, BASELINE, candidate)
    assert after == prefix + '__version__ = "' + candidate + '"' + f"  # keep {BASELINE}\r\n"
    declaration = ast.parse(after).body[-1]
    assert declaration.value.value == candidate
    assert Version.parse(declaration.value.value) > Version.parse(BASELINE)
    assert product["windows_version"] != "1.0.0.10"


@pytest.mark.parametrize("source", [
    "# __version__ = 'old'\n",
    "other = 'old'\n",
    "__version__ = 'old'\n__version__ = 'old'\n",
    "if True:\n    __version__ = 'old'\n",
    "__version__ = 'old'\ndef nested():\n    __version__ = 'old'\n",
    "__version__ = other = 'old'\n",
    "__version__ = 'old'\n__version__ += 'extra'\n",
    "__version__ = 'old'\ndel __version__\n",
    "__version__ = 'old'\ndef __version__():\n    pass\n",
    "__version__ = 'old'\nclass __version__:\n    pass\n",
    "__version__ = 'old'\nfrom other import value as __version__\n",
    "__version__: str = 'old'\n",
])
def test_missing_ambiguous_or_noncanonical_declarations_are_rejected(source):
    with pytest.raises(ValueError, match="one top-level"):
        successor_source(source, "1.0.0", "1.0.1")


@pytest.mark.parametrize("expression", ["make_version()", "'1.0.' + '0'", "42", "f'1.0.0'"])
def test_computed_declaration_is_not_evaluated(expression):
    with pytest.raises(ValueError, match="string literal"):
        successor_source("__version__ = " + expression, "1.0.0", "1.0.1")


def test_old_version_must_match_product_contract():
    with pytest.raises(ValueError, match="Previous backend source version mismatch"):
        successor_source("__version__ = '1.0.0'", "1.0.1", "1.0.2")


@pytest.mark.parametrize("candidate", ["1.0.0", "0.9.0", "1.0.0+same-precedence"])
def test_candidate_must_be_a_genuinely_newer_version(candidate):
    with pytest.raises(ValueError, match="newer"):
        successor_source("__version__ = '1.0.0'", "1.0.0", candidate)
