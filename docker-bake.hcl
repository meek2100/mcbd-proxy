# docker-bake.hcl

# Define variables for reusability.
variable "DOCKER_IMAGE_PREFIX" {
  default = "ghcr.io/meek2100"
}

# The TAGS variable is now a list of strings.
variable "TAGS" {
  default = ["latest"]
}

# Group definitions allow building multiple targets at once.
group "default" {
  targets = ["app"]
}

# A dedicated group for building all images required for CI/testing.
group "ci" {
  targets = ["app-testing", "tester"]
}

# Defines the main production application image.
target "app" {
  context    = "."
  dockerfile = "Dockerfile"
  target     = "final"
  # This now iterates over the TAGS list to apply multiple Docker tags.
  tags       = [for t in TAGS : "${DOCKER_IMAGE_PREFIX}/nether-bridge:${t}"]
  platforms  = [
    "linux/amd64",
    "linux/arm64"
  ]
}

# Defines the 'nether-bridge' image for the testing environment.
target "app-testing" {
  context    = "."
  dockerfile = "Dockerfile"
  target     = "testing"
  tags       = ["nether-bridge:local"]
}

# Defines the 'nb-tester' image for running tests in the CI environment.
target "tester" {
  context    = "."
  dockerfile = "Dockerfile"
  target     = "testing"
  tags       = ["nb-tester:local"]
}