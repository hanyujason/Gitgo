# Stage four combined CI candidate

This is an integration test branch, not an accepted stage-four delivery or a production release.
The acceptance target remains `Truman-Duo/Gitgo:feat/sandbox-rust-stage1-v1`.
Stages two and three are not retargeted, merged or otherwise accepted by this CI run.

## Source composition

- Public development parent: `35ee79b28590f20584ae032cc6a691bd85254ee9`.
- Previously tested local composition: `5c90ce13a376beb3d178237e283161d92441407e`.
- Previous source tree: `b2e78a0ce2f16320711cf9d7e91fb5c778d47507`.
- Previous archive SHA256: `0751425978d4d8aaa03f66aa1a5c24380d7a5799ecc31ca31ef296109e3586fe`.
- Candidate version: `1.0.0-alpha.3.1.1.stage4lab.10`.

The CI snapshot includes the macOS native stage-four work and the updater fixes present
in that local composition. It adds the final helper-entry report parser, portable test
collection, current-bootstrap install assertions, ephemeral sensitive-content fixtures,
and the workflow coverage described below. Personal workstation paths are replaced
with documentation placeholders. Intermediate local build commits are not published.
The CI run head is the exact new snapshot commit; previous VM evidence is not relabelled
as proof of this changed source tree.

## Required checks

The existing Gitgo CI gate retains Windows complete source shards, Dashboard tests,
Windows native/archive/update checks, Linux source/native/update checks on both CPU
architectures, and Bun/Node smoke checks across Windows, macOS and Linux.
The macOS archive build now also requires the stage-four source authority, recovery,
publication and report regression suites. Its JUnit artifact preserves pass/fail/skip
outcomes. The independent archive-acceptance worker exercises both Unix process-scope
and macOS forkless tests using the installed frozen Host, rather than treating their
source-mode skips as acceptance.

macOS capability audit jobs are not privileged helper acceptance. Hosted CI does not
replace the separately recorded SIP-enabled VM administrator, Dashboard approval,
rollback, reboot and cleanup trials. Formal Developer ID publisher identity, trust
provisioning and production capability remain pending Duo; this workflow does not
turn them on and does not publish a Release.

Run with workflow_dispatch on the test branch. The Actions run records the immutable
head SHA and individual platform conclusions. Failed attempts remain evidence and
must not be described as passing; skipped and unverified scenarios remain distinct.
