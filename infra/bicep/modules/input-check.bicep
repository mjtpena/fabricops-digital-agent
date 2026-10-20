// Fails the deployment, with a readable reason, when an input combination cannot work.
//
// Bicep has no assert and cross-parameter validation, so main.bicep computes either 'ok' or a sentence that says
// what is wrong and passes it here. Anything other than 'ok' violates @allowed, so Azure Resource Manager refuses
// this nested deployment and prints the offending value, which is the sentence. The Container App depends on this
// module, so a misconfiguration stops the deployment instead of producing a container that crash-loops.

targetScope = 'resourceGroup'

@description('"ok", or the reason the deployment cannot proceed (shown in the error when it is not "ok").')
@allowed([
  'ok'
])
param result string

@description('Echo of the check result.')
output checked string = result
