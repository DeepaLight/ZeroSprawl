import json
import boto3
import datetime
import os
import logging

# Initialize logger at the module level. This ensures it's available throughout the script.
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables for your AWS resources
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")
DYNAMO_TABLE_NAME = os.environ.get("DDB_TABLE")
# IMPORTANT: The actual model used will be determined by the BEDROCK_MODEL_ID environment variable
# if it's set. Otherwise, it defaults to Amazon Nova Lite.
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.amazon.nova-lite-v1:0")
REGION = os.environ.get("REGION", "us-east-2")

# Initialize AWS clients
dynamodb = boto3.resource('dynamodb')
sns = boto3.client('sns')
bedrock = boto3.client('bedrock-runtime', region_name=REGION)

def lambda_handler(event, context):
    # Log the MODEL_ID being used for debugging. This will appear in your Lambda logs.
    logger.info(f"Using Bedrock Model ID: {MODEL_ID}")

    ALERT_FILE = "alerts_sample.txt"

    # Important: In a Lambda environment, 'alerts_sample.txt' needs to be part of your deployment package.
    # For a production system, you'd typically stream alerts from S3, SQS, or another event source.
    if not os.path.exists(ALERT_FILE):
        logger.error(f"Alert file '{ALERT_FILE}' not found. No alerts will be processed.")
        return {
            "statusCode": 404,
            "body": json.dumps({"message": f"Alert file '{ALERT_FILE}' not found. Please ensure it's in the deployment package."})
        }

    processed_alerts_summary = [] # To collect results for the final response

    with open(ALERT_FILE, "r") as f:
        for line in f:
            try:
                alert_data = json.loads(line.strip())

                # Create a more unique AlertID using microseconds
                alert = {
                    "AlertID": "ALERT-" + datetime.datetime.now().strftime("%Y%m%d%H%M%S%f"),
                    "Severity": alert_data.get("Severity", "Low"),
                    "Source": alert_data.get("Source", "Unknown"),
                    "Message": alert_data.get("Message", ""),
                    "ReceivedAt": datetime.datetime.now().isoformat()
                }

                # Step 1: Enrich the alert using Bedrock (AI summarization, classification, and action guidance)
                # This prompt guides the AI to provide structured JSON output for decision-making.
                bedrock_prompt_template = """
You are an expert security analyst assistant. Your task is to analyze security alerts, summarize them, classify their nature (real threat vs. false positive), determine if they are addressable by an AI agent or require human intervention, and suggest appropriate actions.

Analyze the following security alert:
{alert_message}

Provide your analysis in a JSON format with the following keys. Ensure the output is *only* the JSON object, without any additional text or markdown formatting (e.g., ```json or ```).

- "summary": A one-sentence summary of the alert.
- "is_real_threat": boolean (true if it's a real, addressable threat, false otherwise).
- "action_type": string ("AI_HANDLED", "HUMAN_REQUIRED", "FALSE_POSITIVE", "NON_ADDRESSABLE", "UNKNOWN").
    - "AI_HANDLED": The alert is a real threat and can be fully resolved by an AI agent.
    - "HUMAN_REQUIRED": The alert is a real threat but needs human review or action.
    - "FALSE_POSITIVE": The alert is not a real threat and can be ignored.
    - "NON_ADDRESSABLE": The alert is a real threat but is outside the scope of current automation or requires external action (e.g., vendor patch).
    - "UNKNOWN": If the AI cannot confidently classify or suggest an action.
- "ai_handling_message": string (If action_type is "AI_HANDLED", describe how an AI agent would handle it. Otherwise, an empty string).
- "human_guidance_message": string (If action_type is "HUMAN_REQUIRED", provide clear, actionable guidance for a human analyst. Otherwise, an empty string).

Example for a real threat handled by AI:
{{
    "summary": "Multiple login failures detected for a known bot account, which was automatically blocked.",
    "is_real_threat": true,
    "action_type": "AI_HANDLED",
    "ai_handling_message": "The AI agent automatically blocked the malicious IP address and locked the compromised account. No further action is required.",
    "human_guidance_message": ""
}}

Example for a real threat requiring human:
{{
    "summary": "Malware detected on a critical production server requiring forensic analysis.",
    "is_real_threat": true,
    "action_type": "HUMAN_REQUIRED",
    "ai_handling_message": "",
    "human_guidance_message": "Isolate the server immediately, initiate forensic imaging, and contact the incident response team for further investigation. Do not reboot."
}}

Example for a false positive:
{{
    "summary": "Routine IT vulnerability scan detected as a port scan, confirmed as expected activity.",
    "is_real_threat": false,
    "action_type": "FALSE_POSITIVE",
    "ai_handling_message": "",
    "human_guidance_message": ""
}}
"""
                prompt_text = bedrock_prompt_template.format(alert_message=alert['Message'])

                # --- Dynamic Bedrock Body Construction based on MODEL_ID ---
                # This ensures the request payload matches the specific model's requirements.
                bedrock_body = {}
                if MODEL_ID.startswith("anthropic.claude-3"):
                    # Claude 3 models (Sonnet, Haiku, Opus) use 'messages' and flat parameters.
                    bedrock_body = {
                        "anthropic_version": "bedrock-2023-05-31",
                        "messages": [
                            {"role": "user", "content": [{"type": "text", "text": prompt_text}]}
                        ],
                        "max_tokens": 500,
                        "temperature": 0.2,
                        "top_p": 0.9 # This is correct for Claude 3 models
                    }
                elif MODEL_ID.startswith("anthropic.claude-v2") or MODEL_ID.startswith("anthropic.claude-instant"):
                    # Older Claude models (v2, Instant) use a 'prompt' string and 'max_tokens_to_sample'.
                    bedrock_body = {
                        "prompt": f"\n\nHuman: {prompt_text}\n\nAssistant:",
                        "max_tokens_to_sample": 500,
                        "temperature": 0.2,
                        "top_p": 0.9 # This is correct for older Claude models
                    }
                elif MODEL_ID.startswith("amazon.titan-text"):
                    # Amazon Titan Text models use 'inputText' and 'textGenerationConfig'.
                    bedrock_body = {
                        "inputText": prompt_text,
                        "textGenerationConfig": {
                            "maxTokenCount": 500,
                            "temperature": 0.2,
                            "topP": 0.9 # Note the capital 'P' for Titan models
                        }
                    }
                elif MODEL_ID.startswith("us.amazon.nova-lite"):
                    bedrock_body = {
                        "messages": [
                            {"role": "user", "content": [{"text": prompt_text}]} # Corrected 'prompt' to 'prompt_text'
                        ],
                        "inferenceConfig": {
                            "maxTokens": 150,
                            "temperature": 0.7,
                            "topP": 0.9
                        }
                    }
                else:
                    # Fallback for unknown models. This might still fail, but provides a starting point.
                    # If you encounter issues with a specific model, you'll need to add a dedicated branch above.
                    logger.warning(f"Unknown Bedrock Model ID prefix '{MODEL_ID}'. Attempting to use a generic 'inferenceConfig' structure based on your original code.")
                    bedrock_body = {
                        "messages": [ # Assuming it's a chat-like model, as in your original code
                            {"role": "user", "content": [{"type": "text", "text": prompt_text}]}
                        ],
                        "inferenceConfig": { # As seen in your original code
                            "maxTokens": 500,
                            "temperature": 0.2,
                            "topP": 0.9 # Using capital 'P' as in your original code
                        }
                    }
                # --- End Dynamic Bedrock Body Construction ---

                # Initialize Bedrock response fields with defaults in case of failure
                ai_summary = "AI analysis unavailable at this time."
                is_real_threat = False
                action_type = "ERROR_BEDROCK_FAILED" # Default action type if Bedrock fails
                ai_handling_message = ""
                human_guidance_message = ""
                inference_metadata = {"error": "Bedrock inference not attempted or failed"}

                try:
                    response = bedrock.invoke_model(
                        modelId=MODEL_ID,
                        contentType="application/json",
                        accept="application/json",
                        body=json.dumps(bedrock_body).encode('utf-8') # Encode the dynamically created body
                    )

                    result_raw = response['body'].read().decode('utf-8')
                    
                    # Attempt to parse the JSON output from Bedrock.
                    # Claude models sometimes wrap JSON in markdown blocks (```json ... ```).
                    # This logic tries to extract the pure JSON string.
                    bedrock_output = {}
                    if result_raw.strip().startswith('{') and result_raw.strip().endswith('}'):
                        bedrock_output = json.loads(result_raw)
                    else:
                        try:
                            # Find the first and last curly brace to extract the JSON string
                            json_start = result_raw.find('{')
                            json_end = result_raw.rfind('}')
                            if json_start != -1 and json_end != -1 and json_end > json_start:
                                json_str = result_raw[json_start : json_end + 1]
                                bedrock_output = json.loads(json_str)
                            else:
                                raise ValueError("No valid JSON object found in Bedrock response.")
                        except (json.JSONDecodeError, ValueError) as json_e:
                            logger.error(f"Failed to extract or parse JSON from Bedrock response for AlertID {alert['AlertID']}: {json_e}. Raw response: {result_raw}")
                            # Fallback to empty dict if parsing fails, so .get() calls don't error
                    
                    # --- ADD THIS LINE ---
                    logger.info(f"Bedrock output for AlertID {alert['AlertID']}: {json.dumps(bedrock_output, indent=2)}")
                    # ---------------------

                    # Extract the JSON string from the nested structure
                    # This part needs to be adjusted based on the actual structure of `bedrock_output`
                    # for the specific model being used. The original code's extraction might be too specific.
                    # Assuming for now that `bedrock_output` directly contains the parsed JSON from the prompt.
                    # If models like Nova Lite return a different structure, this will need refinement.
                    
                    # A more robust way to get the content, considering different model outputs:
                    parsed_content = {}
                    if "completion" in bedrock_output: # For older Claude models
                        parsed_content = json.loads(bedrock_output["completion"])
                    elif "content" in bedrock_output and isinstance(bedrock_output["content"], list): # For Claude 3
                        # Assuming the first content block is the text output
                        text_content = bedrock_output["content"][0].get("text", "")
                        parsed_content = json.loads(text_content)
                    elif "results" in bedrock_output and isinstance(bedrock_output["results"], list): # For Titan Text
                        text_content = bedrock_output["results"][0].get("outputText", "")
                        parsed_content = json.loads(text_content)
                    elif "messages" in bedrock_output and isinstance(bedrock_output["messages"], list): # For Nova Lite
                        # Assuming the first message's content is the text output
                        text_content = bedrock_output["messages"][0].get("content", [{}])[0].get("text", "")
                        parsed_content = json.loads(text_content)
                    else:
                        # Fallback if none of the above match, assume bedrock_output *is* the parsed JSON
                        parsed_content = bedrock_output


                    # Extract the classified information, providing sensible defaults
                    ai_summary = parsed_content.get('summary', 'No AI summary generated.')
                    is_real_threat = parsed_content.get('is_real_threat', False)
                    action_type = parsed_content.get('action_type', 'UNKNOWN')
                    ai_handling_message = parsed_content.get('ai_handling_message', '')
                    human_guidance_message = parsed_content.get('human_guidance_message', '')

                    # Capture model metadata
                    inference_metadata = {
                        "model": MODEL_ID,
                        "timestamp": datetime.datetime.now().isoformat(),
                        "raw_bedrock_response": result_raw # Store raw response for debugging
                    }

                except Exception as e:
                    logger.error(f"Bedrock analysis failed for AlertID {alert['AlertID']}: {e}")
                    inference_metadata = {"error": str(e)}

                # 🗄 Step 2: Store alert and AI analysis in DynamoDB
                table = dynamodb.Table(DYNAMO_TABLE_NAME)
                table.put_item(Item={
                    "AlertID": alert["AlertID"],
                    "ReceivedAt": alert["ReceivedAt"],
                    "Severity": alert["Severity"],
                    "Source": alert["Source"],
                    "Message": alert["Message"],
                    "AISummary": ai_summary,
                    "IsRealThreat": is_real_threat,              # AI's determination
                    "ActionType": action_type,                   # AI's suggested action
                    "AIHandlingMessage": ai_handling_message,    # AI's handling message
                    "HumanGuidanceMessage": human_guidance_message, # AI's human guidance
                    # 🏷️ Step 4: Add useful tags / tracking attributes
                    "Status": "Open", # Could be updated to "Resolved" if AI_HANDLED
                    "ProcessedBy": "UASO_Lambda",
                    "Environment": os.environ.get("ENV", "Hackathon"),

                    # Step 5: Store AI model metadata
                    "ModelUsed": inference_metadata.get("model", "unknown"),
                    "InferenceTimestamp": inference_metadata.get("timestamp"),
                    "Metadata": json.dumps(inference_metadata) # Store as JSON string for full context
                })

                # 📢 Step 3: Send alert notification via SNS - Customized Message
                sns_subject = f"[UASO Alert] {alert['Severity']} - {alert['Source']}"
                sns_message_body = f"Alert Summary: {ai_summary}\n\n"

                if action_type == "AI_HANDLED":
                    sns_subject = f"[UASO Alert - AI Handled] {alert['Severity']} - {alert['Source']}"
                    sns_message_body += f"AI Agent Action: {ai_handling_message}\n"
                    sns_message_body += "This threat has been automatically addressed by the AI agent. No human intervention needed."
                elif action_type == "HUMAN_REQUIRED":
                    sns_subject = f"[UASO Alert - Human Required] {alert['Severity']} - {alert['Source']}"
                    sns_message_body += f"Human Guidance: {human_guidance_message}\n"
                    sns_message_body += "Immediate human intervention is required for this threat. Please follow the guidance above."
                elif action_type == "FALSE_POSITIVE":
                    sns_subject = f"[UASO Alert - False Positive] {alert['Severity']} - {alert['Source']}"
                    sns_message_body += "This alert has been classified as a false positive and requires no action. No human intervention needed."
                elif action_type == "NON_ADDRESSABLE":
                    sns_subject = f"[UASO Alert - Non-Addressable] {alert['Severity']} - {alert['Source']}"
                    sns_message_body += "This alert is a real threat but is currently outside the scope of automated handling or requires external action. Manual review is recommended."
                else: # UNKNOWN or ERROR_BEDROCK_FAILED
                    sns_subject = f"[UASO Alert - Action Unknown] {alert['Severity']} - {alert['Source']}"
                    sns_message_body += "The AI could not confidently determine the appropriate action for this alert. Manual review is highly recommended."

                sns_message_body += f"\n\nFull Alert Details:\n{json.dumps(alert, indent=2)}"

                sns.publish(
                    TopicArn=SNS_TOPIC_ARN,
                    Subject=sns_subject,
                    Message=sns_message_body
                )

                logger.info(f"Processed alert {alert['AlertID']} from file. Action Type: {action_type}. Summary: {ai_summary}")
                processed_alerts_summary.append({
                    "AlertID": alert["AlertID"],
                    "summary": ai_summary,
                    "action_type": action_type,
                    "is_real_threat": is_real_threat
                })

            except json.JSONDecodeError as e:
                logger.error(f"Skipping malformed JSON line in '{ALERT_FILE}': {line.strip()} - Error: {e}")
            except Exception as e:
                logger.error(f"An unexpected error occurred while processing a line from '{ALERT_FILE}': {line.strip()} - Error: {e}")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": f"Successfully processed {len(processed_alerts_summary)} alerts from '{ALERT_FILE}'.",
            "processed_alerts": processed_alerts_summary
        })
    }
