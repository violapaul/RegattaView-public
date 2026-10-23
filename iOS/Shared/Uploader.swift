//
//  Uploader.swift
//  BoatStart
//
//  Created by Paul Viola on 11/1/22.
//

import Foundation

struct UrlQuery: Codable {
    var username: String
    var password: String
    var filename: String?
}

struct Uploader {
    /// Upload data to AWS.  First polls the Lambda to create a signed S3 put URL.  Then uploads to that URL.
    static func uploadToAWS(filename: String, data: Data, compress: Bool = true) {
        if compress {
            do {
                let compressedNSData = try (data as NSData).compressed(using: .zlib)
                uploadToAWSHelper(filename: filename, data: (compressedNSData as Data))
            } catch {
                print(error.localizedDescription)
            }
        } else {
            uploadToAWSHelper(filename: filename, data: data)
        }
    }
    
    static func uploadToAWSHelper(filename: String, data: Data) {
        withAWSPutURL(filename: filename, callback: {url in
            print("Uploading to \(redactedURL(url))")
            putData(url, data: data)})
    }

    static func redactedURL(_ urlString: String) -> String {
        guard let url = URL(string: urlString) else {
            return "<invalid URL>"
        }
        return "\(url.host ?? "<unknown host>")\(url.path)"
    }

    /// Retrieve an signed AWS S3 put URL from the lambda endpoint, and then callback to a function that will upload.
    static func withAWSPutURL(filename: String, callback: @escaping (String) -> Void) -> Void {
        let urlString = "https://blsywilg2fba6564nixqxwntyy0iagoj.lambda-url.us-west-2.on.aws/"
        print("Creating AWS Put URL")
        guard let url = URL(string: urlString) else {
            print("Error: cannot create URL")
            return
        }
        
        // The legacy uploader password is no longer in source. Supply it via an
        // Info.plist `LegacyUploaderPassword` entry (e.g. from an untracked
        // xcconfig); without it uploads are skipped. Server-side this identity
        // may only PUT timestamped objects into montlakeupload/.
        guard let password = Bundle.main.object(forInfoDictionaryKey: "LegacyUploaderPassword") as? String,
              !password.isEmpty else {
            print("Upload skipped: LegacyUploaderPassword not configured")
            return
        }
        let query = UrlQuery(username: "peergynt", password: password, filename: filename)
        
        // Create the request
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        
        let jsonEncoder = JSONEncoder()
        do {
            request.httpBody = try jsonEncoder.encode(query)
        }
        catch {
            print("Error Could not json encode")
            return
        }
        
        URLSession.shared.dataTask(with: request) { data, response, error in
            guard error == nil else {
                print("Error: error calling PUT")
                print(error!)
                return
            }
            guard let data = data else {
                print("Error: Did not receive data")
                return
            }
            guard let response = response as? HTTPURLResponse, (200 ..< 299) ~= response.statusCode else {
                print("Error: HTTP request failed")
                return
            }
            do {
                guard let jsonDict = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    print("Error: Cannot convert data to JSON object")
                    return
                }
                if let url = jsonDict["url"] as? String {
                    print("AWS response decoded.")
                    callback(url)
                }
            } catch {
                print("Error: Trying to convert JSON data to string")
                return
            }
        }.resume()
    }

    static func putData(_ urlString: String, data: Data) {
        guard let url = URL(string: urlString) else {
            print("Error: cannot create URL")
            return
        }
        // Create the request
        var request = URLRequest(url: url)
        request.httpMethod = "PUT"
        
        request.httpBody = data
        URLSession.shared.dataTask(with: request) { data, response, error in
            guard error == nil else {
                print("Error: error calling PUT")
                print(error!)
                return
            }
            guard let response = response as? HTTPURLResponse, (200 ..< 299) ~= response.statusCode else {
                print("Error: HTTP request failed")
                return
            }
        }.resume()
    }
}
