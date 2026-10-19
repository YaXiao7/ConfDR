# ChatGPT batch-screening prompt for Stack Overflow posts

```text
[Input format]

You are provided with an Excel file containing Stack Overflow posts.
The worksheet is named "SO Posts Filtered". Each row represents one post in this format:

PostId: """the identifier of the post"""
Title: """the title of the post"""
QuestionBody: """the question body"""
Tags: """the tags of the post"""
AllAnswerContent: """the answers associated with the post"""


[Task description]

Assign exactly one label to every post in the uploaded file.

This study investigates configuration-induced Pod Pending failures: the configuration requirements of a Pod/workload or its dependent resources are incompatible with the cluster state at that time, causing the Pod to fail to complete scheduling or storage binding.

Possible labels are:
{INCLUDE, UNCERTAIN, EXCLUDE}


[Label descriptions]

The "INCLUDE" label is used when a Stack Overflow post discusses a failure consistent with the research object.

The "UNCERTAIN" label is used when the information in the post is insufficient or ambiguous, making it impossible to determine whether the post is consistent with the research object.

The "EXCLUDE" label is used when a Stack Overflow post can be determined not to match the research object.


[Examples]

No examples are provided in the initial screening prompt.


[Input posts]

Read all rows in the uploaded worksheet. For each row, use the Title, QuestionBody, Tags, and AllAnswerContent fields and assign the label independently.


[Output format instructions]

Preserve every original row, column, PostId value, and row order. Add one new column named ChatGPTLabel. Each value in this column must be exactly INCLUDE, UNCERTAIN, or EXCLUDE.

Return the complete result as a downloadable Excel file named `so_posts_screening_result.xlsx`. In the chat response, report the total number of processed posts and the number assigned to each label.
```

