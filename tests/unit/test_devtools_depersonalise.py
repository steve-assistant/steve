"""devtools/depersonalise.py: what it rewrites, what it refuses to guess, and
what it never touches.

It rewrites some 3,000 lines no one will read one by one again, so each
promise is pinned: the grammar holds without conjugating a verb, a "he"
that may be someone else is left for a person, code -- including a
docstring the model may read -- comes out exactly as it went in, and a file
it cannot read is reported rather than passed over. The user here is the
fictional Ezra Quill; the real map is never read by a test.
"""

import ast

import pytest

from devtools import depersonalise

NOBODY = depersonalise.People()
EZRA = depersonalise.People(user=["Ezra Quill", "Ezra"], others=["Quill"])


def _said(text: str, who: depersonalise.People = EZRA) -> str:
    return depersonalise.rewrite(text, who).text


def _left(text: str, who: depersonalise.People = EZRA) -> bool:
    """Whether `text` came out as it went in, with a flag for a person."""
    done = depersonalise.rewrite(text, who)
    return done.text == text and bool(done.flags)


def test_the_user_is_named_without_conjugating_a_verb():
    """"He wants" -> "The user wants": the verb never has to change."""
    assert (_said("# He wants it. Ezra's calendar is his, and his mail waits for him.")
            == "# The user wants it. The user's calendar is theirs, and their mail waits "
               "for them.")
    assert (_said("# -- rules --\n#\n# Ezra, 4 October: yes.")
            == "# -- rules --\n#\n# The user, 4 October: yes.")
    assert _said("Rules:\n\nEzra, 4 October: yes.") == "Rules:\n\nThe user, 4 October: yes."


def test_a_second_he_in_one_sentence_is_they_only_where_the_verb_keeps_its_form():
    """"The user said the user would" reads as two people, but "they owe" is
    a conjugation, and one verb changed beside another left alone ("they are
    about and has") is worse than either."""
    assert _said("He said he would come.") == "The user said they would come."
    assert _said("He said he owes one.") == "The user said the user owes one."
    assert _said("Once he wrote, he is free.") == "Once the user wrote, they are free."
    assert (_said("Once he wrote, he is about and has it.")
            == "Once the user wrote, the user is about and has it.")


def test_his_is_theirs_only_before_punctuation_or_a_conjunction_and_their_otherwise():
    """A list of words that "may" follow a standalone "his" once held verbs
    that are nouns as often, and "his runs" came out "theirs runs"."""
    assert _said("# The call is his, not mine.") == "# The call is theirs, not mine."
    assert _said("# The last word is his.") == "# The last word is theirs."
    assert _said("# It is his and nobody else's.") == "# It is theirs and nobody else's."
    assert (_said("# His runs and his reads are logged.")
            == "# Their runs and their reads are logged.")


def test_a_his_that_is_neither_surely_their_nor_surely_theirs_is_flagged():
    """"his/her" is anyone, "*his*" stresses a word the tool cannot weigh,
    and before "to" or "is" a possessive cannot stand ("his to-do", "a note
    of his is"): any rewrite there is a guess."""
    assert _left("# Ask his/her name.")
    assert _left("# It is *his* call.")
    assert _left("# It is on his to-do list.")
    assert _left("# A note of his is enough.")


def test_a_he_in_quotes_or_beside_another_man_is_flagged_and_left_as_it_is():
    """A quotation is someone's words, and "the recruiter said he would call"
    may be about the recruiter: those are a person's to settle."""
    assert _left('# They asked "is he late?" twice.')
    assert _left("# The recruiter said he would call.")


def test_a_he_beside_a_contact_paired_with_she_or_in_reported_speech_is_flagged():
    """"The contact said he'd call back", rewritten, is a promise the user
    never made, and no reader would catch it. A flag costs a glance. "He or
    she" is anyone, and "s/he" is not even read as a "he"."""
    for role in ("sender", "contact", "caller", "organiser", "organizer", "guest",
                 "recipient", "attendee", "host", "landlord", "doctor", "friend",
                 "colleague", "manager", "recruiter", "professor", "teacher", "partner"):
        assert _left(f"# The {role} said he'd call back."), role
    assert _left("# If he or she replies, file it.")
    assert _left("# If he/she replies, file it.")
    assert _said("# If s/he replies, file it.") == "# If s/he replies, file it."
    assert _left("# The note says that he is away.")


def test_e_g_does_not_end_the_sentence_that_names_someone_else():
    """Read as a sentence's end, "e.g." cut the recruiter off from the "he"
    after it, and the recruiter's call became the user's."""
    assert _left("# The recruiter calls, e.g. he rang on Monday.")
    assert _said("# He pays some, e.g. he would cover rent.") == (
        "# The user pays some, e.g. they would cover rent.")


def test_the_users_name_as_a_greeting_a_sign_off_or_a_word_is_flagged_not_rewritten():
    """"Hi the user," is the failure. A name the user shares with a font or
    a numeral system is a word there, and only a person can tell."""
    georgia = depersonalise.People(user=["Georgia Quill", "Georgia"], others=["Quill"])

    for text in ("# Hi Georgia, the run is done.", "# Thanks, Georgia", "# Thanks,\n# Georgia",
                 "# Pick a serif font, e.g. Georgia.", "# Set it in the Georgia font.",
                 "# Georgia Pro is the bold cut.", "# Georgia numerals are old-style.",
                 "# Georgia, your 3pm moved."):
        assert _left(text, georgia), text
    assert _said("# Georgia wants the 3pm moved.", georgia) == "# The user wants the 3pm moved."


def test_code_and_the_docstrings_the_model_reads_come_out_unchanged():
    """Only comments and developer docstrings change; a tool's docstring is
    its description, rewritten by hand with the prompts. A name left in one
    is flagged, or it would go unreported."""
    source = (
        '"""Ezra asked for this, and `call_ezra` stays."""\n'
        "\n"
        "def register(tool):\n"
        "    @tool\n"
        "    async def look(ctx):\n"
        '        """Ezra reads it."""\n'
        '        return "his"  # he reads it\n'
    )

    done = depersonalise.python(source, EZRA)

    assert done.text.splitlines()[0] == '"""The user asked for this, and `call_ezra` stays."""'
    assert '"""Ezra reads it."""' in done.text
    assert 'return "his"  # the user reads it' in done.text
    assert (6, "a name in a docstring the model may read, left as it is") in done.flags


def test_only_module_and_undecorated_private_function_docstrings_are_rewritten():
    """Bases and decorators once decided which docstrings the model reads,
    and missed a schema built on another schema, a dataclass or Enum handed
    to the model, and a tool registered by name. Whatever could be one of
    those is now left alone."""
    source = (
        '"""He wrote this module."""\n'
        "class Answer(Reply):\n"
        '    """He reads it."""\n'
        "@dataclasses.dataclass\n"
        "class Row:\n"
        '    """He reads it."""\n'
        "class Mood(enum.Enum):\n"
        '    """He reads it."""\n'
        "    def _label(self):\n"
        '        """He reads it."""\n'
        "    def __str__(self):\n"
        '        """He reads it."""\n'
        "def look():\n"
        '    """He reads it."""\n'
        "def _note():\n"
        '    """He reads it."""\n'
        "@cache\n"
        "def _cached():\n"
        '    """He reads it."""\n'
    )

    tree = ast.parse(depersonalise.python(source, NOBODY).text)
    docs = {getattr(n, "name", "module"): ast.get_docstring(n) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef))}

    assert docs == {"module": "The user wrote this module.", "Answer": "He reads it.",
                    "Row": "He reads it.", "Mood": "He reads it.",
                    "_label": "The user reads it.", "__str__": "He reads it.",
                    "look": "He reads it.", "_note": "The user reads it.",
                    "_cached": "He reads it."}


def test_a_module_that_sets_a_doc_keeps_every_docstring():
    """`look.__doc__ = _note.__doc__` makes a private docstring a tool's
    description, and nothing about `_note` itself says so."""
    source = (
        "def _note():\n"
        '    """He reads it."""\n'
        "def look():\n"
        "    pass\n"
        "look.__doc__ = _note.__doc__  # he copies it\n"
    )

    out = depersonalise.python(source, NOBODY).text

    assert out == source.replace("# he copies", "# the user copies")


def test_a_rewrite_that_would_change_the_code_is_refused():
    """The AST check is what makes it safe to run over every package: here a
    stand-in that closes the string it lands in turns a docstring into an
    expression, and the file is refused rather than written."""
    who = depersonalise.People(swaps={"Bo Ek": 'Sam""" + """Ortiz'})

    with pytest.raises(ValueError, match="the code changed"):
        depersonalise.python('"""Bo Ek wrote this."""\nx = 1\n', who)


def test_markdown_code_blocks_are_left_as_they_are():
    out = depersonalise.markdown("He runs it:\n\n```\nhe = 1\n```\n", NOBODY).text

    assert out == "The user runs it:\n\n```\nhe = 1\n```\n"


def test_yaml_comment_lines_are_rewritten_and_a_change_to_the_data_is_refused():
    """A `#` line inside a block scalar is data, not a comment; the tool
    cannot tell by looking, so the file must load the same or stay as it is."""
    done = depersonalise.yaml_comments("# He owns this list.\nitems: [a, b]\n", NOBODY)

    assert done.text == "# The user owns this list.\nitems: [a, b]\n"
    with pytest.raises(ValueError, match="the data changed"):
        depersonalise.yaml_comments("note: |\n  # He wrote this line.\n  and this one\n", NOBODY)


def test_sql_comments_are_rewritten_whole_line_or_trailing_and_strings_are_not():
    """A comment after a column was left with the user's pronouns in it; a
    `--` inside a string or a /* comment */ is not a comment, and rewriting
    it would change the schema."""
    source = ("-- He owns this table.\n"
              "CREATE TABLE notes (\n"
              "  body TEXT DEFAULT 'he -- his',  -- his own words\n"
              "  seen INT  /* it's he */ -- he set it\n"
              ");\n")

    out = depersonalise.sql_comments(source, NOBODY).text

    assert out == ("-- The user owns this table.\n"
                   "CREATE TABLE notes (\n"
                   "  body TEXT DEFAULT 'he -- his',  -- their own words\n"
                   "  seen INT  /* it's he */ -- the user set it\n"
                   ");\n")


def test_every_text_file_it_does_not_rewrite_is_listed_and_a_named_one_fails_the_run(
        tmp_path, capsys):
    """TypeScript, TOML and the rest were passed over in silence, so a name
    in one looked handled. Binary files are passed over quietly."""
    texts = {"note.md": "He runs it.\n", "ui/app.ts": "// Ezra wants this.\n",
             "Cargo.toml": "[package]\n", "core/coder/tools.py": '"""He reads it."""\n'}
    for rel, text in texts.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8", newline="\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\x00Ezra")
    files = sorted(p for p in tmp_path.rglob("*") if p.is_file())

    code = depersonalise.sweep(files, tmp_path, EZRA, write=True)
    lines = [line.split() for line in capsys.readouterr().out.splitlines()
             if line.startswith("SKIPPED ")]

    assert {words[1] for words in lines} == {"ui/app.ts", "Cargo.toml", "core/coder/tools.py"}
    assert {words[1] for words in lines if words[-1] == "MAP"} == {"ui/app.ts"}
    assert code == 1
    assert (tmp_path / "note.md").read_text(encoding="utf-8") == "The user runs it.\n"
    assert (tmp_path / "core/coder/tools.py").read_text(encoding="utf-8") == texts[
        "core/coder/tools.py"]
    assert depersonalise.sweep([p for p in files if p.suffix != ".ts"], tmp_path, EZRA) == 0


def test_the_users_names_come_from_the_maps_user_section_and_no_map_is_an_error(tmp_path):
    """Hard-coded, the user's name was the one real name a public tool
    carried. Read from the map, a run without the map must stop: it would
    otherwise rewrite the pronouns, find no name, and look clean."""
    the_map = tmp_path / "map.tsv"
    the_map.write_text("# --- the user\nEzra Quill\tMaya Lindqvist\nQuill\tLindqvist\n"
                       "ezra@example.com\tmaya@example.com\n"
                       "# --- people\nBo Ek\tSam Ortiz\n", encoding="utf-8", newline="\n")
    who = depersonalise.people(the_map)

    done = depersonalise.rewrite("# Ezra asked Bo Ek to write to Quill.", who)

    assert (who.user, who.others) == (["Ezra Quill", "Ezra"], ["Quill", "ezra@example.com"])
    assert done.text == "# The user asked Sam Ortiz to write to Quill."
    assert any("the user's name" in why for _, why in done.flags)
    with pytest.raises(FileNotFoundError, match="no people map"):
        depersonalise.people(tmp_path / "missing.tsv")
    no_user = tmp_path / "no_user.tsv"
    no_user.write_text("# --- people\nBo Ek\tSam Ortiz\n", encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="names no user"):
        depersonalise.people(no_user)


def test_each_name_is_swapped_once_and_a_full_name_in_either_order(tmp_path):
    """Swapped one map line after another, a stand-in was read again by the
    lines below: "Ingrid" became "Ivy", then "Lowe, Ivy" matched, and one
    real person came out as the stand-in for another."""
    the_map = tmp_path / "map.tsv"
    the_map.write_text("# --- the user\nEzra Quill\tMaya Lindqvist\n"
                       "# --- people\nBo Ek\tSam Ortiz\nBo\tKim\nEk, Kim\tLowe, Ivy\n",
                       encoding="utf-8", newline="\n")
    who = depersonalise.people(the_map)

    done = depersonalise.rewrite("# Exchange writes Ek, Bo; Bo is Bo.", who)

    assert done.text == "# Exchange writes Ortiz, Sam; Kim is Kim."
