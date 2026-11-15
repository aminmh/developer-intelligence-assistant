Here is a curated overview of the best **open-source tools for analyzing, indexing, querying, searching, and manipulating source code**, categorized by their primary role in a modern developer/agentic workflow:

---

### 1. AST Parsing & Semantic Extraction (Code Intelligence)
These tools break source code down into Abstract Syntax Trees (AST) or language-agnostic representations, crucial for structural analysis, refactoring, and AI pipelines.

* **[Tree-sitter](https://github.com/tree-sitter/tree-sitter)**
  * **What it is:** Fast, incremental, multi-language parser generator.
  * **Use cases:** Syntax highlighting, AST querying via S-expressions (Lisp-like queries), building custom code-chunking logic for RAG/embedding pipelines.
  * **Language Support:** Practically every modern language (PHP, Python, JS/TS, Go, Rust, etc.).
* **[Babelfish (bblfsh)](https://github.com/bblfsh/bblfsh)**
  * **What it is:** A self-hosted server for source code parsing into a Universal Abstract Syntax Tree (UAST).
  * **Use cases:** Large-scale cross-language static analysis where you want standardized node types across different programming languages.
* **[SCIP (SCIP Code Intelligence Protocol)](https://github.com/sourcegraph/scip)** & **[LSIF](https://microsoft.github.io/language-server-protocol/)**
  * **What it is:** Rich code-intelligence indexing protocols (originally developed by Sourcegraph).
  * **Use cases:** Pre-indexing cross-references, symbol definitions, and usages to power fast "Jump to Definition" or rich graph-based code exploration without running an active LSP server.

---

### 2. Code Search & Structural Pattern Matching
Go beyond standard regex with tools that understand code grammar and structural tokens.

* **[ast-grep](https://github.com/ast-grep/ast-grep)** (sg)
  * **What it is:** CLI tool and library for searching and rewriting code using AST pattern matching.
  * **Use cases:** Finding structural code patterns (e.g., matching function calls with arbitrary arguments) without complex regex, linting custom anti-patterns, automated codemods.
* **[Semgrep](https://github.com/semgrep/semgrep)** (Core Engine)
  * **What it is:** Fast, lightweight static analysis tool for finding bugs, vulnerabilities, and enforcing code standards using simple YAML rule patterns resembling real code.
  * **Use cases:** Custom security checks, static code analysis, structural code audits.
* **[ripgrep](https://github.com/BurntSushi/ripgrep)** (`rg`)
  * **What it is:** Ultra-fast line-oriented search tool honoring `.gitignore`.
  * **Use cases:** Raw text/regex search over massive repositories.

---

### 3. Repository Search & Code Navigation Engines
Complete self-hosted platforms designed to index, explore, and search multi-repo codebases.

* **[Zoekt](https://github.com/sourcegraph/zoekt)**
  * **What it is:** Trigram-based fast source code search engine (powers the search backend for Sourcegraph and GitLab).
  * **Use cases:** Ultra-fast regex/symbol search over gigabytes of code with low latency.
* **[Livegrep](https://github.com/livegrep/livegrep)**
  * **What it is:** Interactive, real-time regex search engine designed for huge monorepos.
  * **Use cases:** Web-based instant interactive search across millions of lines of code.
* **[Hound](https://github.com/hound-search/hound)**
  * **What it is:** Lightweight, web-based code search tool powered by a Go-based regex indexer.
  * **Use cases:** Simple internal team code search across multiple GitHub/GitLab repositories.

---

### 4. Code Metrics, Graph Analysis & Architecture
Tools for extracting dependencies, call graphs, and architectural structure into data structures or graph databases.

* **[Sourcetrail](https://github.com/CoatiSoftware/Sourcetrail)** *(Open Source Archive / Forked)*
  * **What it is:** Interactive source code explorer that indexes symbols and visualizes call/dependency graphs.
* **[Joern](https://github.com/joernio/joern)**
  * **What it is:** Open-source code analysis platform based on Code Property Graphs (CPG) (combining AST, Control Flow Graph, and Data Flow Graph).
  * **Use cases:** Advanced vulnerability research, data-flow analysis, queryable graph representations of code.
* **[Cloc](https://github.com/AlDanial/cloc)** / **[Tokei](https://github.com/XAMPPRocky/tokei)** / **[scc](https://github.com/boyter/scc)**
  * **What it is:** High-performance lines-of-code, comment, and complexity counters.
  * **Use cases:** Codebase statistics, repo complexity analysis.

---

### 5. Automated Refactoring & Codemods
Tools for systematically changing code structures across large codebases.

* **[Rector](https://github.com/rectorphp/rector)** *(PHP-specific)*
  * **What it is:** Instant automated upgrades and automated refactoring for PHP/Symfony/Laravel codebases using AST mutations.
* **[Comby](https://github.com/comby-tools/comby)**
  * **What it is:** Structural search and replace tool that matches syntax across languages while automatically handling matched parentheses, strings, and delimiters.
  * **Use cases:** Polyglot refactoring and template matching.
* **[Bowler](https://github.com/facebookincubator/Bowler)** / **[LibCST](https://github.com/Instagram/LibCST)** *(Python)*
  * **What it is:** Concrete Syntax Tree parsers and refactoring toolkits that preserve formatting and comments during automated changes.

---

### 6. Code RAG & AI Agent Tooling (Open Source)
If you are indexing code for LLM agents or RAG pipelines:

* **[Repopack / Repomix](https://github.com/yamadashy/repomix)**
  * **What it is:** Packs an entire repository into a structured, token-efficient text/markdown format ready for LLMs.
* **[Aider](https://github.com/paul-gauthier/aider)** (Core Repo-Map Engine)
  * **What it is:** AI pair programmer featuring a Tree-sitter powered repository map that creates a compact high-level semantic representation of symbol definitions and dependencies for LLM context windows.

---