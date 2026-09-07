## Task Overview
This repository contains a logistics shipment platform that has been split into several small services plus one shared library they all build on. The services support shipment intake, carrier synchronization, shipping-label generation, and customer notifications for operations and customer-facing teams. There is no delivery pipeline yet — every commit today ships with no automated build at all — and this is a green-field design, not a repair. The platform, release, and security teams have specific expectations for what a trustworthy pipeline here must do, and those expectations are listed under Objectives. Your job is to design and build the pipeline that meets every one of those expectations.

## Objectives
- The platform team expects a change to one service to only ever rebuild that service, because nobody wants every delivery touching parts of the system nobody changed.
- The release engineer expects every image running anywhere to be traceable back to the exact commit that produced it, because ambiguous floating image names make incident review and rollback unreliable.
- A service owner who has worked with slow delivery systems before expects that changing two unrelated services in the same commit should not mean waiting for one build to finish before the other even starts.
- The on-call engineer expects a change to the shared library to refresh every service that relies on it, because stale shared behavior across services is a common source of production surprises.
- The security review does not want any pull request, no matter how it is opened, to be able to trigger something that behaves like a real deployment or registry release.
- The platform team expects stale work for the same branch to be superseded by newer work, because outdated delivery attempts should not keep consuming runner capacity.

## Helpful Tips
- Get one ordinary build working end to end for a single change before you try to handle every case at once.
- Think through each of the scenarios in the objectives above one at a time, and check your pipeline actually behaves that way, not just that it looks right.
- Think about what trustworthy means for a pipeline that ships to production, not just whether a run reports success.
- Whatever you design should be checkable by hand afterwards, the same way the grading will check it.

## How to Verify
- A change confined to one service should result in exactly one new image, tagged to that commit.
- Two independent services changed in the same commit should build as genuinely overlapping work, not one after another.
- An unrelated documentation or root-level change should produce no new service images for that commit.
- A shared-library-only change should produce a new image for every dependent service.
- Pull request validation should never leave behind evidence that a deployment or registry release was attempted.
- A newer run for the same branch should supersede stale work from an earlier run.
- The grader suite under tests/ exercises exactly these scenarios and runs fully offline against the bundled runner.