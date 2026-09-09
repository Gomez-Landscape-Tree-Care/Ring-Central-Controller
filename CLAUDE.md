# Overview

This stack consists of an AWS Lambda that receives:
1. Webhooks from Ring Central (Inbound/Outbound Calls and SMS)
2. Requests from CL (ClientsLeads) and AB (Applicants Board) Lambdas
and performs:
1. All operations using the Ring Central Service
2. Requests to Slash Backend

## Rules
- Do not deploy, commit, push, or merge anything
- Don't bloat the codebase with unecessary comments