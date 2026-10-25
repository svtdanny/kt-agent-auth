FROM localhost:5001/auth-demo/pod-runner:demo
USER root
RUN usermod --login sandbox --home /sandbox node && groupmod --new-name sandbox node && mkdir /sandbox && chown 1000:1000 /sandbox
COPY pilots/openshell/state-probe.mjs /app/pilot-state.mjs
WORKDIR /sandbox
USER 1000:1000
ENTRYPOINT []
CMD ["node", "/app/agent/server.mjs"]
