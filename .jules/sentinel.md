## 2026-09-24 - PEP 706 Tar Archive Security Extraction Filter
**Vulnerability:** Unfiltered `tar.extractall()` in backup restore manager exposed potential archive extraction risks (such as device creation, mode manipulation, or unexpected symlinks/traversal) and triggered Python deprecation warnings.
**Learning:** Python 3.12+ introduced PEP 706 filter modes (`filter='data'`). Without explicit filter settings, `tarfile.extractall()` issues deprecation warnings and may allow unsafe tar archive attributes to modify extracted files or system permissions.
**Prevention:** Always specify `filter='data'` (with `hasattr(tarfile, 'data_filter')` fallback for legacy Python versions) on all `tarfile.extractall()` invocations.
