import json, pathlib, unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
class PackageContracts(unittest.TestCase):
    def test_required_files(self):
        for f in ["Dockerfile","docker-compose.yml","app/main.py","app/security.py",
                  "app/social.py","app/providers.py",".github/workflows/remote-job.yml"]:
            self.assertTrue((ROOT/f).exists(),f)
    def test_eight_platforms_declared(self):
        s=(ROOT/"app/social.py").read_text()
        for name in ["reddit","facebook","instagram","youtube","x","linkedin","threads","pinterest"]:
            self.assertIn('"'+name+'"',s)
    def test_llm_setup_in_ui(self):
        s=(ROOT/"web/index.html").read_text()
        for v in ["deepseek","openai","gemini","anthropic","API key"]:
            self.assertIn(v,s)
if __name__=="__main__": unittest.main()
