"""Shared CDK asset bundling helpers.

Two Lambda assets need pymongo (not in the Lambda runtime) + the RDS/DocDB CA
bundle baked into the asset:
  - the read-only DocumentDB Mongo collector (data_stack)
  - the operations MCP server, whose DocDB index write (create_docdb_index)
    connects over the Mongo wire protocol (agent_stack). set_docdb_profiler is
    NOT one of them any more: it drives the cluster parameter group + the
    CloudWatch Logs export over the control plane (boto3), no Mongo. pymongo
    still ships because create_docdb_index and the collector need it.

Both reuse `_PipLocalBundling` (Docker-free local pip, Docker fallback) so the
class lives here instead of being duplicated across stack files.
"""

import shutil
import subprocess
from pathlib import Path

import aws_cdk as cdk
import jsii


@jsii.implements(cdk.ILocalBundling)
class _PipLocalBundling:
    """Local bundling fallback for an asset that needs pip dependencies +
    the RDS/DocDB CA bundle.

    CDK's default path bundles inside Docker, but Docker isn't always available
    (CI / the demo host) — and the tests/cdk synth smoke test must stay
    Docker-free. If local `pip` is present we build the asset on the host
    (pip install the linux manylinux wheels for py3.12 + copy source + fetch the
    CA); otherwise try_bundle returns False and CDK falls back to Docker.

    pymongo ships pure-Python with optional C extensions; we install the
    manylinux2014_x86_64 wheels for the Lambda runtime so the host arch/OS
    doesn't leak into the asset.

    `source_dir` must contain a `requirements.txt`. Everything else in
    `source_dir` (handler/package source, any committed global-bundle.pem
    fallback) is copied into the asset.
    """

    def __init__(self, source_dir: str):
        self._source_dir = Path(source_dir).resolve()

    def try_bundle(self, output_dir: str, *_args, **_kwargs) -> bool:
        pip = shutil.which("pip3") or shutil.which("pip")
        if pip is None:
            return False  # no local pip → let CDK use Docker
        out = Path(output_dir)
        try:
            subprocess.run(
                [
                    pip, "install",
                    "-r", str(self._source_dir / "requirements.txt"),
                    "-t", str(out),
                    "--platform", "manylinux2014_x86_64",
                    "--implementation", "cp",
                    "--python-version", "3.12",
                    "--only-binary=:all:",
                    "--upgrade",
                ],
                check=True,
                capture_output=True,
            )
            # Copy the source (everything but requirements.txt) into the asset.
            for item in self._source_dir.iterdir():
                if item.name == "requirements.txt":
                    continue
                dest = out / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)
            # Fetch the RDS/DocDB CA bundle (best-effort: a committed pem in the
            # source dir, copied above, is the fallback when the host is offline).
            curl = shutil.which("curl")
            if curl is not None:
                subprocess.run(
                    [
                        curl, "-fsSL", "-o", str(out / "global-bundle.pem"),
                        "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem",
                    ],
                    check=False,
                    capture_output=True,
                )
            return True
        except (subprocess.CalledProcessError, OSError) as e:
            print(f"[PipLocalBundling] local bundling failed, falling back to Docker: {e}")
            return False
