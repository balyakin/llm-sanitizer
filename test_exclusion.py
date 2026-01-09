import unittest
import logging
import os
import shutil
import tempfile
from obfuscate_runner import ObfuscatingFS, ReplacementEngine

class TestObfuscationExclusion(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.logger = logging.getLogger("test")
        self.logger.setLevel(logging.CRITICAL)
        
        # Create dummy file structure
        os.makedirs(os.path.join(self.test_dir, ".git"), exist_ok=True)
        with open(os.path.join(self.test_dir, ".git", "config"), "w") as f:
            f.write("git config data")
            
        os.makedirs(os.path.join(self.test_dir, "src"), exist_ok=True)
        with open(os.path.join(self.test_dir, "src", "main.py"), "w") as f:
            f.write("print('Hello')")

        os.makedirs(os.path.join(self.test_dir, "venv"), exist_ok=True)
        with open(os.path.join(self.test_dir, "venv", "bin"), "w") as f:
            f.write("binary data")

        # Setup replacements
        self.replacements = [("Hello", "Goodbye"), ("main", "secondary")]
        self.replacer = ReplacementEngine(self.replacements, self.logger)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_exclusion_patterns(self):
        excludes = [".git", "venv"]
        fs = ObfuscatingFS(self.test_dir, self.replacer, [".py"], self.logger, excludes=excludes)

        # Test _is_excluded directly
        # Note: _is_excluded takes a real absolute path
        
        # 1. Root should not be excluded
        self.assertFalse(fs._is_excluded(self.test_dir))
        
        # 2. .git directory should be excluded
        git_path = os.path.join(self.test_dir, ".git")
        self.assertTrue(fs._is_excluded(git_path))
        
        # 3. File inside .git should be excluded (because parent is excluded)
        git_config_path = os.path.join(self.test_dir, ".git", "config")
        self.assertTrue(fs._is_excluded(git_config_path))
        
        # 4. src directory should NOT be excluded
        src_path = os.path.join(self.test_dir, "src")
        self.assertFalse(fs._is_excluded(src_path))
        
        # 5. File inside src should NOT be excluded
        main_py_path = os.path.join(self.test_dir, "src", "main.py")
        self.assertFalse(fs._is_excluded(main_py_path))
        
        # 6. venv directory should be excluded
        venv_path = os.path.join(self.test_dir, "venv")
        self.assertTrue(fs._is_excluded(venv_path))

    def test_readdir_obfuscation(self):
        excludes = [".git", "venv"]
        fs = ObfuscatingFS(self.test_dir, self.replacer, [".py"], self.logger, excludes=excludes)

        # Listing root
        # .git -> excluded, so name should NOT be obfuscated (even if it matched something, which it doesn't here)
        # src -> not excluded, so if it matched something it WOULD be obfuscated. 
        # But let's check behavior. 
        # The logic in readdir says: if entry matches exclude pattern, return it raw.
        
        entries = list(fs.readdir("/", 0))
        self.assertIn(".git", entries)
        self.assertIn("venv", entries)
        self.assertIn("src", entries) # "src" doesn't match replacement rules anyway

    def test_readdir_inside_excluded(self):
        excludes = [".git"]
        fs = ObfuscatingFS(self.test_dir, self.replacer, [".py"], self.logger, excludes=excludes)
        
        # Mocking that we are inside .git
        # readdir takes a relative path from mount root
        # path=".git"
        
        # If we list .git, we expect 'config' to be returned as is, even if 'config' was in replacements
        # Let's add 'config' to replacements to verify
        replacer = ReplacementEngine([("config", "confused")], self.logger)
        fs = ObfuscatingFS(self.test_dir, replacer, [".py"], self.logger, excludes=excludes)
        
        entries = list(fs.readdir(".git", 0))
        self.assertIn("config", entries)
        self.assertNotIn("confused", entries)

    def test_content_obfuscation_check(self):
        # Verify that _is_text_path respects exclusions
        excludes = ["src"]
        fs = ObfuscatingFS(self.test_dir, self.replacer, [".py"], self.logger, excludes=excludes)
        
        # src/main.py is a .py file, so normally it would be text.
        # But since 'src' is excluded, _is_text_path should return False (treated as binary/passthrough)
        
        main_py_path = os.path.join(self.test_dir, "src", "main.py")
        self.assertFalse(fs._is_text_path(main_py_path))
        
        # Now without exclusion
        fs_no_exclude = ObfuscatingFS(self.test_dir, self.replacer, [".py"], self.logger, excludes=[])
        self.assertTrue(fs_no_exclude._is_text_path(main_py_path))

if __name__ == "__main__":
    unittest.main()