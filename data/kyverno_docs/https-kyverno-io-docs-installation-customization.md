---
source_url: https://kyverno.io/docs/installation/customization/
kyverno_version: unversioned
title: Customizing Kyverno | Kyverno
---

# Configuring Kyverno

## Customizing Kyverno

[Section titled “Customizing Kyverno”](#customizing-kyverno)

Kyverno has many different functions and supports a wide range of possible customizations. This section provides more information on Kyverno’s supporting resources and how they can be customized to tune certain behaviors.

### Certificate Management

[Section titled “Certificate Management”](#certificate-management)

The Kyverno policy engine runs as an admission webhook and requires a CA-signed certificate and key to setup secure TLS communication with the Kubernetes API server. There are two ways to configure secure communications between Kyverno and the API server.

#### Default certificates

[Section titled “Default certificates”](#default-certificates)

By default, Kyverno will automatically generate a self-signed Certificate Authority (CA) and leaf certificates for use in its webhook registrations. The CA certificate expires after one year. When Kyverno manages its own certificates, it will gracefully handle regeneration upon expiry. By default, RSA 2048-bit keys are used, but other [key algorithms](#key-algorithm) can be configured.

After installing Kyverno, use the [step CLI](https://smallstep.com/cli/) to check and verify certificate details.

Get all Secrets in Kyverno’s Namespace. The Secret names are configurable, see the [container flags section](/docs/installation/customization#container-flags) for more details.

Terminal window

```
$ kubectl -n kyverno get secret



NAME                                                      TYPE                 DATA   AGE



kyverno-cleanup-controller.kyverno.svc.kyverno-tls-ca     kubernetes.io/tls    2      21d



kyverno-cleanup-controller.kyverno.svc.kyverno-tls-pair   kubernetes.io/tls    2      21d



kyverno-svc.kyverno.svc.kyverno-tls-ca                    kubernetes.io/tls    2      21d



kyverno-svc.kyverno.svc.kyverno-tls-pair                  kubernetes.io/tls    2      21d
```

Get and decode the CA certificate used by the admission controller.

Terminal window

```
$ kubectl -n kyverno get secret kyverno-svc.kyverno.svc.kyverno-tls-ca -o jsonpath='{.data.tls\.crt}' | \



step base64 -d | step certificate inspect --short



X.509v3 Root CA Certificate (RSA 2048) [Serial: 0]



Subject:     *.kyverno.svc



Issuer:      *.kyverno.svc



Valid from:  2023-04-14T18:33:37Z



to:  2024-04-13T19:33:37Z
```

Get and decode the certificate used to register the webhooks with the Kubernetes API server (assumes at least one validate policy is installed) and see the same CA root certificate is in use.

Terminal window

```
$ kubectl get validatingwebhookconfiguration kyverno-resource-validating-webhook-cfg -o jsonpath='{.webhooks[0].clientConfig.caBundle}' | \



step base64 -d | step certificate inspect --short



X.509v3 Root CA Certificate (RSA 2048) [Serial: 0]



Subject:     *.kyverno.svc



Issuer:      *.kyverno.svc



Valid from:  2023-04-14T18:33:37Z



to:  2024-04-13T19:33:37Z
```

#### Certificates rotation

[Section titled “Certificates rotation”](#certificates-rotation)

By default, Kyverno will generate and manage certificates. CA certificates validity is one year and TLS certificates validity is 150 days.

At a minimum, managed certificates are checked for validity every 12 hours. Additionally, validity checks are performed when events occur on secrets containing the managed certificates.

The renewal process runs as follows:

1. Remove expired certificates contained in the secret
2. Check if remaining certificates will become invalid in less than 15 days
3. If needed, generate a new certificate with the validity documented above
4. The new certificate is added to the underlying secret along with current certificates that are still valid
5. Reconfigure webhooks with the new certificates bundle
6. Update the Kyverno server to use the new certificate

Basically, certificates will be renewed approximately 15 days before expiry.

#### Key Algorithm

[Section titled “Key Algorithm”](#key-algorithm)

When Kyverno manages its own certificates (the default behavior), you can configure the cryptographic algorithm used to generate the CA and TLS certificates. The supported algorithms are:


### Webhook-related RBAC

[Section titled “ClusterRoles”](#clusterroles)

Kyverno creates the following ClusterRoles, one per controller type:

* `kyverno:admission-controller`
  + get CustomResourceDefinitions to perform sanity checks.
  + get, list, watch, create, update, patch and delete MutatingWebhookConfigurations to configure webhook rules for admission mutations.
  + get, list, watch, create, update, patch and delete ValidatingWebhookConfigurations to configure webhook rules for admission validations.
  + get, list, watch, create, update, patch and delete ValidatingAdmissionPolicies for auto-generating validatingadmissionpolicies.
  + get, list, watch, create, update, patch and delete ValidatingAdmissionPolicyBindings for auto-generating validatingadmissionpolicybindings.
  + get, list and watch Roles to manage webhook configurations auto-deletion.
  + get, list and watch ClusterRoles manage webhook configurations auto-deletion.
  + get, list and watch RoleBindings manage webhook configurations auto-deletion.
  + get, list and watch ClusterRolebindings manage webhook configurations auto-deletion.
* `kyverno:reports-controller`
  + get CustomResourceDefinitions to perform sanity checks.
  + get, list, watch, create, update, patch and delete ValidatingAdmissionPolicies for auto-generating validatingadmissionpolicies.
  + get, list, watch, create, update, patch and delete ValidatingAdmissionPolicyBindings for auto-generating validatingadmissionpolicybindings.
* `kyverno:background-controller`
  + get CustomResourceDefinitions to perform sanity checks.
* `kyverno:cleanup-controller`
  + get CustomResourceDefinitions to perform sanity checks.
  + get, list, watch, create, update and delete ValidatingWebhookConfigurations to perform resources deletion based on TTL cleanup label.

Kyverno uses [aggregated ClusterRoles](https://kubernetes.io/docs/reference/access-authn-authz/rbac/#aggregated-clusterroles) to search for and combine ClusterRoles which apply to Kyverno. Each controller has its own set of ClusterRoles. Those ending in `core` are the aggregate ClusterRoles which are then aggregated by the top-level role without the `core` suffix.

