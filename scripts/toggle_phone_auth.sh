#!/bin/bash

# ==============================================================================
# StyleStack: Toggle Phone Auth Feature
#
# Use this script to dynamically hide or show the "Continue with Phone" button 
# on the frontend without needing to release a new app update.
# ==============================================================================

# 1. Set your backend URL (change to your production URL when deployed)
API_URL="https://stylestack-be.onrender.com/api/v1"

# 2. Set your secret Admin API Key (This must match the ADMIN_API_KEY env variable on the backend)
ADMIN_KEY="rounak_is_admin"

# 3. Choose whether to enable or disable (true or false)
ENABLE_PHONE_AUTH="false"  


echo "Toggling phone authentication to: $ENABLE_PHONE_AUTH"

curl -X POST "$API_URL/config/features" \
     -H "Content-Type: application/json" \
     -H "x-admin-api-key: $ADMIN_KEY" \
     -d '{
           "phone_auth_enabled": '$ENABLE_PHONE_AUTH'
         }'

echo -e "\n\nDone! Restart the Flutter app to see the changes."
