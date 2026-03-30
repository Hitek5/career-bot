import asyncio
import logging
import tempfile
import os

logger = logging.getLogger(__name__)

CLAUDE_CMD = "claude"
CLAUDE_MODEL = "claude-sonnet-4-20250514"


async def call_claude(system_prompt: str, user_message: str,
                      max_tokens: int = 4096) -> str:
    """Call Claude via CLI. Combines system+user into one prompt file.
    
    Runs from /tmp to avoid CLAUDE.md pickup.
    """
    # Combine into single prompt
    combined = f"[SYSTEM INSTRUCTIONS]\n{system_prompt}\n[END SYSTEM]\n\n{user_message}"

    # Write to temp file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.prompt.txt',
                                      dir='/tmp', delete=False,
                                      encoding='utf-8') as f:
        f.write(combined)
        prompt_file = f.name

    try:
        # Read file content via shell and pass to -p
        cmd = [
            CLAUDE_CMD, "--print",
            "--model", CLAUDE_MODEL,
            "--permission-mode", "auto",
            "-p", combined,
        ]

        logger.info("Calling Claude CLI (model=%s, combined_len=%d)",
                    CLAUDE_MODEL, len(combined))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/tmp",
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=180,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("Claude CLI timeout (180s)")

        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[:500]
            logger.error("Claude CLI failed (rc=%d): %s", proc.returncode, err)
            raise RuntimeError(f"Claude CLI error: {err}")

        result = stdout.decode(errors="replace").strip()
        if not result:
            raise RuntimeError("Claude CLI returned empty response")

        logger.info("Claude CLI response: %d chars", len(result))
        return result

    finally:
        try:
            os.unlink(prompt_file)
        except OSError:
            pass
