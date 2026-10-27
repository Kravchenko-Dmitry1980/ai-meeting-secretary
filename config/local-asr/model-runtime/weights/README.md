# Stage 2 weights

`provision_weights.py` uses Python standard-library HTTPS to retrieve the three immutable official Hugging Face weight URLs. It verifies pinned sizes and SHA-256 before exposing the final weight filenames, copies the handoff metadata byte-for-byte, and verifies all 3378 original handoff files before and after provisioning. It never imports model libraries or activates inference.

Run from the Secretary workspace using the existing isolated test interpreter:

```powershell
.\.runtime\local-asr-test-runtime-20261026\Scripts\python.exe -B config\local-asr\model-runtime\weights\provision_weights.py
```

The result is `.runtime/local-asr-models-20261027/{rnnt,ctc,nemotron}` and a receipt under `.runtime/model-pilot-evidence-20261027/weights-*.json`. Only after all three models pass, `contracts/` contains byte-identical copies of the six original contracts except the RNNT revision typo: `7655ad717f8122257385bb4b2f373db3697e8686` becomes the verified official `7655ad717f8122257385bb4b2f373db3697e8680`. File hashes and sizes inside that model manifest remain unchanged. The original package manifest remains a record of the original source package and is not rewritten.

For a fresh offline verification receipt:

```powershell
.\.runtime\local-asr-test-runtime-20261026\Scripts\python.exe -B config\local-asr\model-runtime\weights\provision_weights.py --verify-only
```

Limits: each network operation has a 45-second socket timeout, each weight has a 1800-second overall download deadline, response size cannot exceed the pinned length, and redirects are restricted to HTTPS Hugging Face domains. Failed partial downloads are preserved with `.part` suffix and block automatic retry until reviewed. Existing completed files must match their contracts. No credentials are supplied. This is local artifact verification, not model quality or production qualification.
