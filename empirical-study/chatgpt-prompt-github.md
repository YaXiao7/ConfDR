# ChatGPT batch-screening prompt for GitHub issues

```text
[Input format]

You are provided with an Excel file containing GitHub issues.
The worksheet is named "Core 30 Repos". Each row represents one issue in this format:

Repo: """the repository containing the issue"""
IssueNo: """the issue number within the repository"""
Title: """the title of the issue"""
Body: """the body of the issue"""
Labels: """the labels assigned to the issue"""


[Task description]

Assign exactly one label to every issue in the uploaded file.

This study investigates configuration-induced Pod Pending failures: the configuration requirements of a Pod/workload or its dependent resources are incompatible with the cluster state at that time, causing the Pod to fail to complete scheduling or storage binding.

Possible labels are:
{INCLUDE, UNCERTAIN, EXCLUDE}


[Label descriptions]

The "INCLUDE" label is used when a GitHub issue discusses a failure consistent with the research object.

The "UNCERTAIN" label is used when the information in the issue is insufficient or ambiguous, making it impossible to determine whether the issue is consistent with the research object.

The "EXCLUDE" label is used when a GitHub issue can be determined not to match the research object.


[Cause attribution rule]

The presence of Pod Pending, FailedScheduling, an unsatisfied scheduling predicate, or a storage-binding failure is not sufficient by itself for the issue to be labeled INCLUDE.

Assign INCLUDE only when the failure is caused by a configuration requirement of the Pod/workload or its dependent resources being incompatible with the actual cluster state.

Assign EXCLUDE when the failure is primarily caused by an implementation defect, regression, race condition, stale state, incorrect internal behavior, or erroneous scheduling/binding decision of Kubernetes or a related platform component (e.g., scheduler, controller, autoscaler, CSI driver, operator), rather than by an unsatisfiable configuration requirement.

Do not classify an issue as EXCLUDE merely because it was submitted to an official or upstream repository. Repository ownership is not a screening criterion; determine the label based on the root cause described in the issue.

If the available information is insufficient to determine whether the root cause is configuration incompatibility or a platform implementation defect, assign UNCERTAIN.


[Examples]

No examples are provided in the initial screening prompt.


[Input issues]

Read all rows in the uploaded worksheet. For each row, use the Repo, Title, Body, and Labels fields and assign the label independently.


[Output format instructions]

Preserve every original row, column, Repo value, IssueNo value, and row order. Add one new column named ChatGPTLabel. Each value in this column must be exactly INCLUDE, UNCERTAIN, or EXCLUDE.

Return the complete result as a downloadable Excel file named `github_issues_screening_result.xlsx`. In the chat response, report the total number of processed issues and the number assigned to each label.
```

