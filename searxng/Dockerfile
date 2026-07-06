# Thin wrapper over the official image: ship our settings.yml so JSON is enabled
# and the limiter is off. Placing the file at /etc/searxng/settings.yml makes the
# entrypoint use it instead of generating one from the template.
FROM searxng/searxng:latest
COPY settings.yml /etc/searxng/settings.yml
