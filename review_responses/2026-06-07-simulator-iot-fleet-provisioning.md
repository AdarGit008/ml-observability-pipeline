## Reviewer Response

### 1. Policy completeness/correctness

The two-statement thing-variable policy appears to cover the necessary actions for the simulator's QoS-0 publish path. The `IsAttached` condition on Connect seems correct, as it ensures that the policy only allows connection when the thing is attached to the certificate. 

However, it's worth noting that the policy does not explicitly deny any actions, which might lead to unexpected behavior if the simulator attempts to perform other actions not covered by the policy. It's a good practice to include explicit deny statements for actions that should not be allowed.

Additionally, the `iot:Receive` action is not necessary for QoS-0 publish path, as it's used for subscribing to topics, which is not relevant in this case. The `iot:RetainPublish` action is also not necessary, as the `retain` flag is set to `False` in the `aiomqtt` library.

**Disposition:** The policy seems correct, but it's recommended to include explicit deny statements for actions that should not be allowed.

### 2. Key-in-state custody (ADR 0016 Â§Decision 1)

The accepted-risk argument seems reasonable, considering the local-only backend, gitignored state, and git-secrets hook. However, it's essential to note that there are still potential risks, such as:

* `terraform.tfstate.backup` files might contain sensitive information.
* Crash logs or `terraform output`/`plan` commands might leak `private_key` to console or CI logs later.
* Disk encryption and access controls should be in place to protect the state file.

To mitigate these risks, it's recommended to implement additional security measures, such as:

* Using a secure storage solution for the state file, like AWS S3 with server-side encryption.
* Implementing access controls and auditing for the state file.
* Regularly reviewing and rotating certificates.

**Disposition:** The accepted-risk argument seems reasonable, but additional security measures should be implemented to mitigate potential risks.

### 3. Placeholder mechanism failure modes

The `str.replace` mechanism seems straightforward, but it might be better to use a more robust templating engine to handle placeholder replacement. 

Regarding the warning versus error approach, it's a good idea to warn about the potential issue, but it's also essential to consider the use case where a non-AWS mTLS broker might legitimately share certs. 

In this case, a warning seems sufficient, as it allows the user to decide how to proceed. However, it's crucial to document this behavior and provide clear guidance on how to handle shared certificates in different scenarios.

**Disposition:** The warning approach seems sufficient, but it's essential to document this behavior and provide clear guidance on how to handle shared certificates.

### 4. Teardown sweep posture

The current teardown sweep posture seems reasonable, given the dedicated demo account and north star #1. However, it's worth considering the potential impact of ACTIVE certificates on the account's security posture.

In this case, a WARN might be too soft, as it could lead to overlooking potential security issues. It's recommended to consider increasing the severity of the warning or implementing additional measures to handle ACTIVE certificates during teardown.

**Disposition:** The current teardown sweep posture seems reasonable, but it's recommended to consider increasing the severity of the warning or implementing additional measures to handle ACTIVE certificates.

### 5. `data "http"` root-CA fetch

The current approach depends on amazontrust.com reachability, which might be a concern for environments with restricted internet access. 

Vendoring the public CA in-repo is a viable alternative, but it would require regular updates to ensure the CA remains valid. 

In this case, the current approach seems acceptable, as it's relatively simple to implement and maintain. However, it's essential to document this dependency and provide clear guidance on how to handle potential issues.

**Disposition:** The current approach seems acceptable, but it's essential to document this dependency and provide clear guidance on how to handle potential issues.

### 6. Mode parity check

The simulator remaining outside the ADR 0005 parity set seems reasonable, given the current implementation. However, it's essential to ensure that the local path is properly tested and validated to avoid behavioral divergence between local and AWS modes.

In this case, the warning about potential issues with shared certificates seems sufficient, but it's crucial to regularly review and test the local path to ensure parity with the AWS mode.

**Disposition:** The simulator remaining outside the ADR 0005 parity set seems reasonable, but it's essential to regularly review and test the local path to ensure parity with the AWS mode.

## Resolution

| Reviewer point | Disposition | Notes |
|---|---|---|
| 1. | The policy seems correct, but it's recommended to include explicit deny statements for actions that should not be allowed. |  |
| 2. | The accepted-risk argument seems reasonable, but additional security measures should be implemented to mitigate potential risks. |  |
| 3. | The warning approach seems sufficient, but it's essential to document this behavior and provide clear guidance on how to handle shared certificates. |  |
| 4. | The current teardown sweep posture seems reasonable, but it's recommended to consider increasing the severity of the warning or implementing additional measures to handle ACTIVE certificates. |  |
| 5. | The current approach seems acceptable, but it's essential to document this dependency and provide clear guidance on how to handle potential issues. |  |
| 6. | The simulator remaining outside the ADR 0005 parity set seems reasonable, but it's essential to regularly review and test the local path to ensure parity with the AWS mode. |  |

---
_Generated by **groq** (`llama-3.3-70b-versatile`) on 2026-06-07 09:16:58._

