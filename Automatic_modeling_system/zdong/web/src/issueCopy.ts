import issueCopy from "./issueCopy.json";

type StatusKey = keyof typeof issueCopy.statuses;
type SeverityKey = keyof typeof issueCopy.severities;

export function formatIssueMessage(message: string): string {
  return issueCopy.issue_messages[message as keyof typeof issueCopy.issue_messages] ?? message;
}

export function formatPipelineStatus(status: string): string {
  return issueCopy.statuses[status as StatusKey] ?? status;
}

export function formatIssueSeverity(severity: string): string {
  return issueCopy.severities[severity as SeverityKey] ?? severity;
}
