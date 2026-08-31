#!/bin/bash

# ==============================================================================
# StyleStack: Toggle Google Calendar Sync Feature
#
# Use this script to dynamically hide or show the "Connect Google Calendar" 
# feature on the frontend while waiting for Google's Trust & Safety review.
# ==============================================================================

# 1. Set your backend URL (change to your production URL when deployed)
API_URL="https://stylestack-be.onrender.com/api/v1"

# 2. Set your secret Admin API Key (This must match the ADMIN_API_KEY env variable on the backend)
ADMIN_KEY="rounak_is_admin"

# 3. Choose whether to enable or disable (true or false)
ENABLE_CALENDAR="false"  


echo "Toggling Google Calendar Sync to: $ENABLE_CALENDAR"

curl -X POST "$API_URL/config/features" \
     -H "Content-Type: application/json" \
     -H "x-admin-api-key: $ADMIN_KEY" \
     -d '{
           "google_calendar_enabled": '$ENABLE_CALENDAR'
         }'

echo -e "\n\nDone! Restart the Flutter app to see the changes."
