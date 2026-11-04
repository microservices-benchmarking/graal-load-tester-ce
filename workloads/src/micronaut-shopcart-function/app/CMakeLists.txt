# Copyright (c) 2026, Oracle and/or its affiliates. All rights reserved.
set(TEST_NAME "micronaut-shopcart-function")
file(GLOB_RECURSE NATIVE_IMAGE_SOURCES *.gradle src/*)

add_custom_target(test-apps-common-java-${TEST_NAME} DEPENDS musl native/nativeCompile/${TEST_NAME})
add_gradle_native_image(${TEST_NAME} SOURCES micronaut-cli.yml ${NATIVE_IMAGE_SOURCES})
graalos_add_test_app(test-apps-common-java-${TEST_NAME})
