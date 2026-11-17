services:
  - type: web
    name: kd-video-compressor-bot
    runtime: docker
    plan: free
    dockerfilePath: ./Dockerfile
    healthCheckPath: /healthz
    envVars:
      - key: BOT_TOKEN
        sync: false
