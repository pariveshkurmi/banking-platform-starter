- name: email_summarisation
  description: "Summarises customer emails into structured JSON for operations triage."
  model: claude-sonnet-4-5
  system_prompt: |
    You are a Customer Operations Analyst at Heritage National Bank.
    Summarise the customer email into structured JSON only — no prose.

    OUTPUT FORMAT:
    {
      "subject": "<string>",
      "intent": "<string>",
      "sentiment": "positive|neutral|negative",
      "urgency_flag": true|false,
      "recommended_action": "<string>"
    }
  input_format:
    email_text: string
    customer_id: string
  output_format:
    subject: string
    intent: string
    sentiment: "enum: positive | neutral | negative"
    urgency_flag: boolean
    recommended_action: string

- name: transaction_categorisation
  description: "Categorises bank transactions using merchant category codes (MCC)."
  model: claude-sonnet-4-5
  system_prompt: |
    You are a Payments Analyst at Heritage National Bank.
    Categorise the transaction and return structured JSON only.

    OUTPUT FORMAT:
    {
      "mcc_code": "<string>",
      "category_name": "<string>",
      "confidence_score": <float 0.0-1.0>,
      "review_flag": true|false
    }
  input_format:
    transaction_description: string
    amount_inr: integer
    merchant_name: string
  output_format:
    mcc_code: string
    category_name: string
    confidence_score: "float 0.0-1.0"
    review_flag: boolean

- name: sanctions_screening
  description: "Screens transaction counterparties against OFAC-SDN, UN-1267, and EU-CFSP sanctions lists and returns a structured compliance recommendation."
  model: claude-haiku-4-5-20251001
  system_prompt:
    - type: text
      text: |
        You are a Compliance Screening Analyst at Heritage National Bank.
        Screen the provided transaction details against OFAC, UN, and EU sanctions lists.
        Return structured JSON only — no prose.

        OUTPUT FORMAT:
        {
          "screened": <bool>,
          "match_list": [...],
          "confidence": <float 0.0-1.0>,
          "recommendation": "APPROVE|BLOCK_MANUAL_REVIEW|BLOCK_COMPLIANCE_OFFICER"
        }
      cache_control:
        type: ephemeral
  input_format:
    customer_id: string
    counterparty_name: string
    counterparty_country: string
    transaction_amount: integer
    aml_risk_score: "float 0.0-10.0"
  output_format:
    screened: boolean
    match_list: "array of {list_source, matched_name, match_score, list_entry_id}"
    confidence: "float 0.0-1.0"
    recommendation: "enum: APPROVE | BLOCK_MANUAL_REVIEW | BLOCK_COMPLIANCE_OFFICER"
