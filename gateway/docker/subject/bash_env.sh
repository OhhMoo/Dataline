# The subject container's bash, forwarded to the episode container.
#
# Read by every bash the CLI starts: as $BASH_ENV for `bash -c`, and from
# /etc/profile.d for `bash -lc`. A shell that was given a command hands it to
# rexec.py, which runs it in the episode container at /workspace; this bash is
# replaced by the client and exits with the command's status.
#
# The CLI keeps its working directory by putting a `cd <its cwd>` in the
# command, and that directory only exists here; it is rewritten to /workspace
# before the command crosses. ALKERA_SUBJECT_CWD is a name chosen to appear in
# no real command, so the rewrite touches nothing else.
#
# Unlike the host topology's hook, unsetting BASH_ENV is not an escape that
# matters: the shell it leaves you in is this container, which holds the CLI,
# an empty home, and no route anywhere but the API proxy (tasks/harness/README.md#the-subject-sandbox).
if [ -n "${BASH_EXECUTION_STRING:-}" ]; then
  __alkera_cmd=${BASH_EXECUTION_STRING//"$ALKERA_SUBJECT_CWD"//workspace}
  exec /usr/local/bin/python /opt/alkera/rexec.py client "$__alkera_cmd"
fi
