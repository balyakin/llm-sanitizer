# TODO: Preparation for Public Release

## 🚨 Critical (Security & Privacy)
- [x] **Sanitize Shell Scripts:** Remove absolute paths (e.g., `/Users/evgenybalyakin/...`) from `gemini_obfuscated.sh` and others. Replace them with dynamic paths using `$(pwd)` or relative paths.
- [x] **Sanitize Python Code:** Check `obfuscate_runner.py` for any hardcoded local paths or user-specific logic.
- [x] **Secure Data Handling:**
    - Rename `sensitive_data.json` to `sensitive_data.example.json` and replace real names with generic placeholders (e.g., "API_KEY", "ProjectName").
    - Ensure `sensitive_data.json` is added to `.gitignore`.
    - Ensure `.obfuscation_map.json` is in `.gitignore`.
- [x] **Sanitize Tests:** Check `test_exclusion.py` or other tests for hardcoded paths.

## 📦 Packaging & Installation
- [x] **Create `requirements.txt`:** Add `fusepy`.
- [x] **Create `setup.py` or `pyproject.toml`:** (Optional but recommended) Make the project installable as a package.
- [x] **Update `.gitignore`:** Ensure it covers:
    - `.venv/`
    - `__pycache__/`
    - `*.log`
    - `obf_mounts/`
    - `sensitive_data.json` (the real one)

## 🛠 Refactoring & Usability
- [x] **Unify Shell Wrappers:** Instead of 4 separate scripts (`gemini_*.sh`, `claude_*.sh`, etc.), create a single entry point script (e.g., `run_safe.sh`) that accepts the command as an argument: `./run_safe.sh gemini`.
- [x] **CLI Help:** Ensure `obfuscate_runner.py --help` returns clear, formatted usage instructions. (Provided by argparse).

## 📄 Documentation
- [x] **Finalize README.md:** Remove personal paths, add generic installation steps, add License info.
- [x] **Add LICENSE:** Create a standard license file (MIT recommended).
