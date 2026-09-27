# Security

The supplied Compose stack binds HTTP ports to loopback and is intended for local evaluation. Before remote exposure, configure TLS, per-operator identity, scoped and rotated credentials, broker authentication/ACLs, network policy, a dedicated migration account and a tested backup/restore procedure. See the design and deployment documentation for the remaining production gates.

Report vulnerabilities privately to the repository owner's security contact. This new repository does not yet have a published reporting address; do not publish secrets or customer observations in a public issue. Rotate compromised credentials and preserve audit evidence.

The generated `.env` contains random local credentials and is ignored by Git. Root database credentials are passed only to MySQL and initialization; Grafana admin credentials are passed only to Grafana and the test profile. Application services receive application credentials. Splitting the shared application database role into per-stage privileges is a production-hardening requirement. Grafana has SELECT access only to reporting views. Investigation pages never embed or persist a write key.
