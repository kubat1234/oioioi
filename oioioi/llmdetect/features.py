"""Source code features used by the LLM detection.

Pure functions, no Django: a small lexer, stylometric features describing
how the code is written (not what it does) and winnowing fingerprints of
the normalized token stream, which survive renaming variables, changing
literals, removing comments and reformatting.
"""

import math
import re
import zlib
from collections import Counter

# Bump when the features or fingerprints change, so stored ones get recomputed.
FEATURES_VERSION = 5

# Bounds on what is stored per submission, so that crafted code cannot make
# its stored analysis (and the memory to assess a contest) large.
MAX_FINGERPRINTS = 4000
MAX_LINE_HASHES = 5000

C_FAMILY = "c"
PYTHON = "python"
PASCAL = "pascal"

_EXTENSIONS = {
    "c": C_FAMILY,
    "cc": C_FAMILY,
    "cpp": C_FAMILY,
    "cxx": C_FAMILY,
    "h": C_FAMILY,
    "hpp": C_FAMILY,
    "java": C_FAMILY,
    "py": PYTHON,
    "pas": PASCAL,
}

C_KEYWORDS = frozenset(
    """
    auto bool break case catch char class const constexpr continue default delete do double else enum explicit
    extern false float for friend goto if inline int long namespace new nullptr operator private protected public
    register return short signed sizeof static static_cast struct switch template this throw true try typedef
    typename union unsigned using virtual void volatile while final override import package boolean extends
    implements interface instanceof
    """.split()
)

PYTHON_KEYWORDS = frozenset(
    """
    False None True and as assert async await break class continue def del elif else except finally for from global
    if import in is lambda nonlocal not or pass raise return try while with yield
    """.split()
)

PASCAL_KEYWORDS = frozenset(
    """
    and array begin case const div do downto else end file for function goto if in label mod nil not of or packed
    procedure program record repeat set then to type until var while with integer longint int64 boolean char string
    real readln writeln read write
    """.split()
)

# Library names kept as they are in fingerprints: they carry how the
# solution is built, and nobody renames them to hide copying.
LIBRARY_NAMES = frozenset(
    """
    std cin cout cerr endl printf scanf puts getchar putchar vector map set multiset unordered_map unordered_set pair
    queue priority_queue deque stack string array bitset tuple make_pair make_tuple emplace_back push_back pop_back
    emplace insert erase find count begin end rbegin rend size empty clear resize reserve assign front back top push
    pop first second sort stable_sort reverse unique lower_bound upper_bound binary_search min max swap abs fill memset
    accumulate iota next_permutation greater less min_element max_element ios sync_with_stdio tie getline
    to_string stoi stoll long_long numeric_limits main list dict range len print input int str sys stdin readline
    split append extend sorted heapq heappush heappop deque defaultdict
    """.split()
)

# Frequent words of English code comments; Polish participants usually
# write comments in Polish or not at all. Words that are Polish too (a, to,
# we, do, na...) are left out.
ENGLISH_WORDS = frozenset(
    """
    the an of and or is are for each if this in with from read compute calculate number store check update use
    return find all that it be by as at not then else loop over until while into new current result answer
    initialize count value values total maximum minimum first last next previous array list edge edges node nodes
    graph tree query queries case cases input output print step using get add remove handle since must will can need
    so only otherwise iterate element elements index position length size given
    """.split()
)

_WORD_RE = re.compile(r"[A-Za-zÀ-ž]+")
_CAMEL_RE = re.compile(r"[a-z][A-Z]")
_NUMBER_RE = re.compile(r"(0[xX][0-9a-fA-F]+|\d+\.?\d*([eE][+-]?\d+)?)[uUlLfF]*")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Names in other alphabets (Polish letters, text pasted with the code): a
# separate pattern, so that ASCII names are read exactly as before.
_UNICODE_IDENT_RE = re.compile(r"[^\W\d]\w*")
_OPERATORS = sorted(
    "<<= >>= ... -> ++ -- << >> <= >= == != && || += -= *= /= %= &= |= ^= :: := ** // ->".split(),
    key=len,
    reverse=True,
)
_BINARY_OPS_RE = re.compile(r"(?<![=!<>+\-*/%&|^:])(==|!=|<=|>=|\+=|-=|\*=|/=|&&|\|\||=|\+|-|\*|/|%|<|>)(?![=+\-&|>])")
# Participants control this text: every pattern must run in linear time
# (bounded repetitions, matches starting at word boundaries).
_CODE_IN_COMMENT_RE = re.compile(r"(;\s*$|\bcout\b|\bprintf\b|\bcerr\b|\bprint\(|\bfor\s*\(|\bif\s*\(|\b\w+\s*\([^()\n]{0,200}\)\s*;|\b\w+\s*=\s*[\w(])")
# Comments longer than this are only looked at up to it.
COMMENT_LIMIT = 1000

FINGERPRINT_K = 12
FINGERPRINT_WINDOW = 6


def language_of(filename):
    """Language family of a source file, by extension (C-family by default)."""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _EXTENSIONS.get(extension, C_FAMILY)


def keywords_of(language):
    return {C_FAMILY: C_KEYWORDS, PYTHON: PYTHON_KEYWORDS, PASCAL: PASCAL_KEYWORDS}[language]


def lex(source, language):
    """Splits ``source`` into tokens and comments.

    Returns ``(tokens, comments)``: tokens are ``(kind, text)`` with kind
    one of ``id``, ``num``, ``str``, ``op``, ``pp`` (preprocessor line);
    comments are the comment texts, without the comment markers.
    """
    tokens = []
    comments = []
    i = 0
    n = len(source)
    at_line_start = True
    while i < n:
        c = source[i]
        if c == "\n":
            at_line_start = True
            i += 1
            continue
        if c.isspace():
            i += 1
            continue
        two = source[i : i + 2]
        if language == PYTHON:
            if c == "#":
                end = source.find("\n", i)
                end = n if end == -1 else end
                comments.append(source[i + 1 : end])
                i = end
                continue
            triple = source[i : i + 3]
            if triple in ('"""', "'''"):
                end = source.find(triple, i + 3)
                end = n if end == -1 else end + 3
                text = source[i + 3 : end - 3]
                # A string standing alone on its line is a docstring.
                if at_line_start:
                    comments.append(text)
                else:
                    tokens.append(("str", text))
                i = end
                at_line_start = False
                continue
        else:
            if two == "//":
                end = source.find("\n", i)
                end = n if end == -1 else end
                comments.append(source[i + 2 : end])
                i = end
                continue
            if two == "/*" or (language == PASCAL and two == "(*"):
                closing = "*/" if two == "/*" else "*)"
                end = source.find(closing, i + 2)
                end = n if end == -1 else end + 2
                comments.append(source[i + 2 : end - 2])
                i = end
                continue
            if language == PASCAL and c == "{":
                end = source.find("}", i + 1)
                end = n if end == -1 else end + 1
                comments.append(source[i + 1 : end - 1])
                i = end
                continue
            if c == "#" and at_line_start and language == C_FAMILY:
                end = i
                # Preprocessor lines continue after a backslash.
                while True:
                    end = source.find("\n", end)
                    if end == -1:
                        end = n
                        break
                    if source[end - 1] != "\\":
                        break
                    end += 1
                tokens.append(("pp", source[i:end].strip()))
                i = end
                continue
        at_line_start = False
        if c in "\"'":
            j = i + 1
            while j < n and source[j] != c and source[j] != "\n":
                j += 2 if source[j] == "\\" else 1
            tokens.append(("str", source[i + 1 : j]))
            i = j + 1
            continue
        # isdigit() and isalpha() are true for more than the patterns match
        # ("²", "①", "ę"): such characters fall through to the operators.
        match = None
        if c.isdigit():
            match = _NUMBER_RE.match(source, i)
            kind = "num"
        elif c.isalpha() or c == "_":
            match = _IDENT_RE.match(source, i) or _UNICODE_IDENT_RE.match(source, i)
            kind = "id"
        if match:
            tokens.append((kind, match.group(0)))
            i = match.end()
            continue
        for op in _OPERATORS:
            if source.startswith(op, i):
                tokens.append(("op", op))
                i += len(op)
                break
        else:
            tokens.append(("op", c))
            i += 1
    return tokens, comments


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else 0.0


def _is_prose(comment):
    words = _WORD_RE.findall(comment)
    letters = sum(len(w) for w in words)
    return len(words) >= 3 and letters >= 0.6 * len(comment.strip()) and not _CODE_IN_COMMENT_RE.search(comment)


def _is_sentence(comment):
    text = comment.strip().lstrip("*/ ").strip()
    words = _WORD_RE.findall(text)
    return len(words) >= 4 and text[:1].isupper() and _is_prose(text)


def _identifiers(tokens, language):
    keywords = keywords_of(language)
    return [text for kind, text in tokens if kind == "id" and text not in keywords and text not in LIBRARY_NAMES]


def _indent_unit(lines):
    widths = Counter()
    for line in lines:
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if indent and stripped and not line.startswith("\t"):
            for unit in (8, 4, 3, 2):
                if indent % unit == 0:
                    widths[unit] += 1
                    break
            else:
                widths[1] += 1
    if not widths:
        return 0.0
    return float(widths.most_common(1)[0][0])


def extract_features(source, language):
    """Stylometric features of ``source``, as a dict of floats.

    Ratios are in [0, 1]; counts are per 100 non-blank lines so that long
    and short solutions are comparable.
    """
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    nonblank = [line for line in lines if line.strip()]
    loc = len(nonblank)
    per_100 = 100.0 / max(loc, 1)
    tokens, comments = lex(source, language)
    comments = [c[:COMMENT_LIMIT] for c in comments if c.strip()]
    texts = [text for _kind, text in tokens]
    identifiers = _identifiers(tokens, language)
    distinct = set(identifiers)
    pp_lines = [text for kind, text in tokens if kind == "pp"]
    comment_words = [w.lower() for c in comments for w in _WORD_RE.findall(c)]
    words_in_prose = [w.lower() for c in comments if _is_prose(c) for w in _WORD_RE.findall(c)]
    code_text = "\n".join(nonblank)
    brace_lines = [line.strip() for line in nonblank if "{" in line]
    binary_ops = list(_BINARY_OPS_RE.finditer(code_text))
    spaced_ops = 0
    for match in binary_ops:
        before = code_text[match.start() - 1 : match.start()]
        after = code_text[match.end() : match.end() + 1]
        spaced_ops += before in (" ", "\t") and after in (" ", "\t", "\n")
    indented = [line for line in nonblank if line[:1] in (" ", "\t")]
    statements_per_line = [line.count(";") for line in nonblank if not line.lstrip().startswith("for")]
    counts = Counter(texts)
    has_main = "main" in counts
    # std::x, not the std of "using namespace std".
    std_prefixes = sum(1 for (kind, text), following in zip(tokens, tokens[1:], strict=False) if kind == "id" and text == "std" and following == ("op", "::"))

    features = {
        # Comments.
        "comment_ratio": min(1.0, _ratio(len(comments), loc)),
        "prose_comment_ratio": _ratio(sum(_is_prose(c) for c in comments), len(comments)),
        "sentence_comment_ratio": _ratio(sum(_is_sentence(c) for c in comments), len(comments)),
        "english_comment_ratio": _ratio(sum(w in ENGLISH_WORDS for w in words_in_prose), len(words_in_prose)),
        "commented_code_ratio": _ratio(sum(bool(_CODE_IN_COMMENT_RE.search(c)) for c in comments), len(comments)),
        "comment_words_per_100": len(comment_words) * per_100,
        # Identifiers.
        "ident_mean_length": _ratio(sum(len(i) for i in identifiers), len(identifiers)),
        "long_ident_ratio": _ratio(sum(len(i) >= 6 for i in distinct), len(distinct)),
        "multiword_ident_ratio": _ratio(sum(("_" in i.strip("_")) or bool(_CAMEL_RE.search(i)) for i in distinct), len(distinct)),
        "short_ident_ratio": _ratio(sum(len(i) <= 2 for i in distinct), len(distinct)),
        "upper_ident_ratio": _ratio(sum(i.isupper() and len(i) > 1 for i in distinct), len(distinct)),
        # Idioms of competitive programmers and of LLMs.
        "std_qualified_per_100": std_prefixes * per_100,
        "using_namespace_std": float(bool(re.search(r"using\s+namespace\s+std", source))),
        "bits_header": float(any("bits/stdc++" in line for line in pp_lines)),
        "includes_per_100": sum(line.startswith("#include") for line in pp_lines) * per_100,
        "defines_per_100": sum(line.startswith("#define") for line in pp_lines) * per_100,
        "type_aliases_per_100": (counts["typedef"] + len(re.findall(r"\busing\s+\w+\s*=", source))) * per_100,
        "fast_io": float("sync_with_stdio" in counts),
        "debug_traces_per_100": (counts["cerr"] + sum("DEBUG" in line or "#ifdef" in line for line in pp_lines)) * per_100,
        "return_zero": float(has_main and bool(re.search(r"return\s+0\s*;\s*\}\s*$", source.strip()))),
        "endl_ratio": _ratio(counts["endl"], counts["endl"] + source.count("\\n")),
        "printf_ratio": _ratio(counts["printf"] + counts["scanf"], counts["printf"] + counts["scanf"] + counts["cout"] + counts["cin"]),
        "const_per_100": (counts["const"] + counts["constexpr"]) * per_100,
        "auto_per_100": counts["auto"] * per_100,
        "long_long_spelled_per_100": len(re.findall(r"\blong\s+long\b", source)) * per_100,
        "reference_params_per_100": len(re.findall(r"\bconst\s+[\w:<>, ]{1,80}+&", source)) * per_100,
        # Layout.
        "brace_own_line_ratio": _ratio(sum(line == "{" for line in brace_lines), len(brace_lines)),
        "tab_indent_ratio": _ratio(sum(line.startswith("\t") for line in indented), len(indented)),
        "indent_unit": _indent_unit(nonblank),
        "blank_line_ratio": _ratio(len(lines) - loc, len(lines)),
        "mean_line_length": _ratio(sum(len(line.rstrip()) for line in nonblank), loc),
        "spaced_operator_ratio": _ratio(spaced_ops, len(binary_ops)),
        "multi_statement_line_ratio": _ratio(sum(count > 1 for count in statements_per_line), len(statements_per_line)),
        "trailing_space_ratio": _ratio(sum(line != line.rstrip() for line in nonblank), loc),
    }
    return features


# Features compared with the author's own style. Counts per 100 lines are
# log-scaled, so a few extra comments in a short program do not dominate.
STYLE_FEATURES = (
    "comment_ratio",
    "prose_comment_ratio",
    "sentence_comment_ratio",
    "english_comment_ratio",
    "commented_code_ratio",
    "comment_words_per_100",
    "ident_mean_length",
    "long_ident_ratio",
    "multiword_ident_ratio",
    "short_ident_ratio",
    "upper_ident_ratio",
    "std_qualified_per_100",
    "using_namespace_std",
    "bits_header",
    "includes_per_100",
    "defines_per_100",
    "type_aliases_per_100",
    "fast_io",
    "debug_traces_per_100",
    "return_zero",
    "endl_ratio",
    "printf_ratio",
    "const_per_100",
    "auto_per_100",
    "long_long_spelled_per_100",
    "reference_params_per_100",
    "brace_own_line_ratio",
    "tab_indent_ratio",
    "indent_unit",
    "blank_line_ratio",
    "mean_line_length",
    "spaced_operator_ratio",
    "multi_statement_line_ratio",
    "trailing_space_ratio",
)


def style_vector(features):
    """The style vector of a submission, from :func:`extract_features`."""
    vector = []
    for name in STYLE_FEATURES:
        value = features.get(name, 0.0)
        if name.endswith("_per_100") or name in ("ident_mean_length", "mean_line_length", "indent_unit"):
            value = math.log1p(value)
        vector.append(value)
    return vector


# How typical a feature is of LLM-written competitive code, as (weight,
# feature, cap): the feature is divided by the cap and clipped to [0, 1].
# Hand-set from what LLM chats produce for olympiad problems; once the
# staff has made enough decisions, these are worth refitting. Only features
# that set LLM code apart from code taught at school: "return 0;" and
# spaces around operators are what teachers teach, so they are not here.
LLM_STYLE_WEIGHTS = (
    (1.6, "sentence_comment_ratio", 0.5),
    (1.0, "english_comment_ratio", 0.4),
    (0.8, "comment_words_per_100", 60.0),
    (1.0, "multiword_ident_ratio", 0.4),
    (0.6, "long_ident_ratio", 0.5),
    (0.8, "std_qualified_per_100", 10.0),
    (0.5, "reference_params_per_100", 3.0),
    (0.4, "const_per_100", 6.0),
    (-0.8, "using_namespace_std", 1.0),
    (-0.8, "bits_header", 1.0),
    (-0.8, "defines_per_100", 4.0),
    (-0.6, "type_aliases_per_100", 3.0),
    (-1.0, "debug_traces_per_100", 2.0),
    (-1.0, "commented_code_ratio", 0.3),
    (-0.8, "short_ident_ratio", 0.8),
    (-0.6, "multi_statement_line_ratio", 0.2),
)
LLM_STYLE_BIAS = -2.0


def llm_style(features):
    """How much the code looks LLM-written, in [0, 1], with the features
    that contributed most, as ``(score, [(feature, contribution), ...])``.
    """
    contributions = []
    logit = LLM_STYLE_BIAS
    for weight, name, cap in LLM_STYLE_WEIGHTS:
        value = min(1.0, max(0.0, features.get(name, 0.0) / cap))
        contribution = weight * value
        logit += contribution
        if contribution:
            contributions.append((name, contribution))
    contributions.sort(key=lambda item: -abs(item[1]))
    return 1.0 / (1.0 + math.exp(-logit)), contributions


def normalized_tokens(source, language):
    """Token stream with user identifiers, numbers and strings replaced by
    placeholders, and without comments and preprocessor lines.
    """
    tokens, _comments = lex(source, language)
    keywords = keywords_of(language)
    result = []
    for kind, text in tokens:
        if kind == "pp":
            continue
        if kind == "id":
            result.append(text if text in keywords or text in LIBRARY_NAMES else "I")
        elif kind == "num":
            result.append("N")
        elif kind == "str":
            result.append("S")
        else:
            result.append(text)
    return result


def fingerprints(source, language, k=FINGERPRINT_K, window=FINGERPRINT_WINDOW):
    """Winnowing fingerprints (Schleimer et al., as in MOSS) of the
    normalized token stream: a sorted list of distinct ints.
    """
    tokens = normalized_tokens(source, language)
    if len(tokens) < k:
        return [zlib.crc32(" ".join(tokens).encode())] if tokens else []
    hashes = [zlib.crc32(" ".join(tokens[i : i + k]).encode()) for i in range(len(tokens) - k + 1)]
    if len(hashes) <= window:
        return sorted(set(hashes))[:MAX_FINGERPRINTS]
    selected = set()
    for i in range(len(hashes) - window + 1):
        selected.add(min(hashes[i : i + window]))
    # The smallest hashes, as in MinHash: every document keeps the same part
    # of the hash space, so containment between capped sets stays meaningful.
    return sorted(selected)[:MAX_FINGERPRINTS]


def line_hashes(source):
    """Hashes of the non-blank lines without whitespace, to count how many
    lines one version of a program adds to another.
    """
    hashes = []
    for line in source.splitlines():
        if line.strip():
            hashes.append(zlib.crc32("".join(line.split()).encode()))
            if len(hashes) == MAX_LINE_HASHES:
                break
    return hashes


def code_lines(source):
    """Non-blank lines that are not includes, defines or ``using`` lines."""
    count = 0
    for line in source.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("#include", "#define", "using ", "import ", "from ")):
            count += 1
    return count


def containment(fingerprints_a, fingerprints_b):
    """Fraction of fingerprints of A that also occur in B."""
    if not fingerprints_a:
        return 0.0
    b = fingerprints_b if isinstance(fingerprints_b, (set, frozenset)) else set(fingerprints_b)
    return sum(1 for f in fingerprints_a if f in b) / len(fingerprints_a)


def analyze_source(source, filename):
    """Everything stored for one piece of code."""
    language = language_of(filename)
    features = extract_features(source, language)
    return {
        "language": language,
        "features": features,
        "fingerprints": fingerprints(source, language),
        "line_hashes": line_hashes(source),
        "code_lines": code_lines(source),
    }
