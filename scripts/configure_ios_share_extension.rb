require "xcodeproj"

project_path = File.expand_path("../../stylestack_fe/ios/Runner.xcodeproj", __dir__)
project = Xcodeproj::Project.open(project_path)
runner = project.targets.find { |target| target.name == "Runner" }
abort("Runner target not found") unless runner

share = project.targets.find { |target| target.name == "ShareExtension" }
unless share
  share = project.new_target(:app_extension, "ShareExtension", :ios, "13.0")
  runner.add_dependency(share)

  group = project.main_group.find_subpath("ShareExtension", true)
  group.set_source_tree("<group>")
  swift = group.new_file("ShareViewController.swift")
  group.new_file("Info.plist")
  group.new_file("ShareExtension.entitlements")
  share.source_build_phase.add_file_reference(swift)

end

embed = runner.copy_files_build_phases.find { |phase| phase.name == "Embed App Extensions" }
embed ||= runner.new_copy_files_build_phase("Embed App Extensions")
runner.build_phases << embed unless runner.build_phases.include?(embed)
embed.dst_subfolder_spec = "13"
embed.add_file_reference(share.product_reference) unless embed.files_references.include?(share.product_reference)

runner.build_configurations.each do |config|
  config.build_settings["CODE_SIGN_ENTITLEMENTS"] = "Runner/Runner.entitlements"
  config.build_settings["CUSTOM_GROUP_ID"] = "group.com.stylestack.stylestack.share"
end

share.build_configurations.each do |config|
  config.build_settings.merge!(
    "APPLICATION_EXTENSION_API_ONLY" => "YES",
    "CODE_SIGN_ENTITLEMENTS" => "ShareExtension/ShareExtension.entitlements",
    "CURRENT_PROJECT_VERSION" => "1",
    "CUSTOM_GROUP_ID" => "group.com.stylestack.stylestack.share",
    "GENERATE_INFOPLIST_FILE" => "NO",
    "INFOPLIST_FILE" => "ShareExtension/Info.plist",
    "IPHONEOS_DEPLOYMENT_TARGET" => "13.0",
    "MARKETING_VERSION" => "1.0",
    "PRODUCT_BUNDLE_IDENTIFIER" => "com.stylestack.stylestack.ShareExtension",
    "PRODUCT_NAME" => "$(TARGET_NAME)",
    "SKIP_INSTALL" => "YES",
    "SWIFT_VERSION" => "5.0",
    "TARGETED_DEVICE_FAMILY" => "1,2"
  )
end

unless share.package_product_dependencies.any? { |item| item.product_name == "receive-sharing-intent" }
  package = project.new(Xcodeproj::Project::Object::XCLocalSwiftPackageReference)
  package.relative_path = "Flutter/ephemeral/Packages/.packages/receive_sharing_intent-1.9.0"
  project.root_object.package_references << package

  dependency = project.new(Xcodeproj::Project::Object::XCSwiftPackageProductDependency)
  dependency.package = package
  dependency.product_name = "receive-sharing-intent"
  share.package_product_dependencies << dependency

  build_file = project.new(Xcodeproj::Project::Object::PBXBuildFile)
  build_file.product_ref = dependency
  share.frameworks_build_phase.files << build_file
end

project.save
puts "Configured ShareExtension target and App Group."
