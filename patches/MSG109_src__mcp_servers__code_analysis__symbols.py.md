# Патч: src/mcp_servers/code_analysis/symbols.py

Исходный заголовок: `## 📄 src/mcp_servers/code_analysis/symbols.py — **ЗАМЕНИТЬ** блоки`
Сообщение: MSG 109, строка 81549

```python
    parsers = {
        ".py": ("tree_sitter_python", "language"),
        ".js": ("tree_sitter_javascript", "language"),
        ".jsx": ("tree_sitter_javascript", "language"),
        ".mjs": ("tree_sitter_javascript", "language"),
        ".cjs": ("tree_sitter_javascript", "language"),
        ".ts": ("tree_sitter_typescript", "language_typescript"),
        ".tsx": ("tree_sitter_typescript", "language_tsx"),
    }
```

```python
    parsers = {
        ".py": ("tree_sitter_python", "language"),
        ".js": ("tree_sitter_javascript", "language"),
        ".jsx": ("tree_sitter_javascript", "language"),
        ".mjs": ("tree_sitter_javascript", "language"),
        ".cjs": ("tree_sitter_javascript", "language"),
        ".ts": ("tree_sitter_typescript", "language_typescript"),
        ".tsx": ("tree_sitter_typescript", "language_tsx"),
        ".go": ("tree_sitter_go", "language"),
        ".rs": ("tree_sitter_rust", "language"),
        ".java": ("tree_sitter_java", "language"),
        ".cs": ("tree_sitter_c_sharp", "language"),
        ".php": ("tree_sitter_php", "language_php"),
        ".rb": ("tree_sitter_ruby", "language"),
    }
```

```python
NODE_KINDS = {
    "python": {
        "function_definition": "function",
        "async_function_definition": "function",
        "class_definition": "class",
        "decorated_definition": None,  # обёртка
    },
    "javascript": {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
        "lexical_declaration": "variable",
    },
    "typescript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "abstract_class_declaration": "class",
        "interface_declaration": "interface",
        "method_definition": "method",
        "lexical_declaration": "variable",
        "type_alias_declaration": "type",
        "enum_declaration": "enum",
    },
}
```

```python
NODE_KINDS = {
    "python": {
        "function_definition": "function",
        "async_function_definition": "function",
        "class_definition": "class",
        "decorated_definition": None,  # обёртка
    },
    "javascript": {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
        "lexical_declaration": "variable",
    },
    "typescript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "abstract_class_declaration": "class",
        "interface_declaration": "interface",
        "method_definition": "method",
        "lexical_declaration": "variable",
        "type_alias_declaration": "type",
        "enum_declaration": "enum",
    },
    "go": {
        "function_declaration": "function",
        "method_declaration": "method",
        "type_declaration": "type",
        "const_declaration": "const",
        "var_declaration": "variable",
    },
    "rust": {
        "function_item": "function",
        "struct_item": "struct",
        "enum_item": "enum",
        "trait_item": "trait",
        "impl_item": "impl",
        "mod_item": "module",
        "type_item": "type",
    },
    "java": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "method_declaration": "method",
        "constructor_declaration": "constructor",
        "record_declaration": "record",
    },
    "c_sharp": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "struct_declaration": "struct",
        "enum_declaration": "enum",
        "method_declaration": "method",
        "record_declaration": "record",
    },
    "php": {
        "function_definition": "function",
        "method_declaration": "method",
        "class_declaration": "class",
        "interface_declaration": "interface",
        "trait_declaration": "trait",
    },
    "ruby": {
        "method": "method",
        "class": "class",
        "module": "module",
        "singleton_method": "method",
    },
}
```

```python
def detect_language(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".py":
        return "python"
    if ext in (".js", ".jsx", ".mjs", ".cjs"):
        return "javascript"
    if ext in (".ts", ".tsx"):
        return "typescript"
    return ""
```

```python
def detect_language(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".py":
        return "python"
    if ext in (".js", ".jsx", ".mjs", ".cjs"):
        return "javascript"
    if ext in (".ts", ".tsx"):
        return "typescript"
    if ext == ".go":
        return "go"
    if ext == ".rs":
        return "rust"
    if ext == ".java":
        return "java"
    if ext == ".cs":
        return "c_sharp"
    if ext == ".php":
        return "php"
    if ext == ".rb":
        return "ruby"
    return ""
```
