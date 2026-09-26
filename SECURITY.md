# Security

The brokers are laboratory tools for a single-host software radio. By
default they bind only loopback ZMQ endpoints, accept only loopback TCP or
absolute IPC endpoint overrides, and expose arming through a UNIX socket in
a private (mode 0700) directory, authenticated by peer credentials and a
per-run 256-bit token that is never printed. `radio_fault.py stop` signals a
broker only after re-checking the recorded process identity.

Please report a suspected vulnerability privately, through GitHub's private
vulnerability reporting for this repository if it is enabled, or otherwise
by contacting the maintainer listed in [AUTHORS.md](AUTHORS.md). Include the
affected revision, the command or input, and whether reproducing it needs a
running broker. Do not include subscriber credentials, captures or host
details in a public issue.

Operational notes:

- Run the brokers as an ordinary user; they need no privileges.
- Do not expose the ZMQ ports beyond loopback: the ZMQ radio protocol has no
  authentication.
- A trial directory holds its control token until the run ends. Remove
  `control.token` before sharing or archiving a trial.
- The example srsUE configuration contains public srsRAN test credentials.
  Never reuse them for a real subscriber or network.
