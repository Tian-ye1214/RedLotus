# Third-party notices for the Mambo CPU worker

This inventory describes the Windows x64 worker SHA256 `4f8bcbfd5f96456cca05b8e0b8b172475b64ef21940a08940d0703695bbd608e`. 
Its build starts from GPT-SoVITS-cpp commit `384a2f995c48ef1903373ac8814569a8925779ba`,
with RedLotus changes recorded in `upstream.patch`. The upstream Apache-2.0
terms are in [UPSTREAM-LICENSE.txt](UPSTREAM-LICENSE.txt);
[NOTICE.md](NOTICE.md) describes the adaptation.

The Release link inputs are recorded in the pinned build's
`redlotus_mambo.vcxproj` (AdditionalDependencies) and Rust target fingerprints.
The lists below are conservative: a link input may contain code that the
linker discards. License texts are reproduced from the exact local
submodule sources or official versioned crates.io archives; each Rust
archive was checked against the SHA256 in the pinned Cargo.lock.

## Native C and C++ components

| Component | Pinned source | License copies |
| --- | --- | --- |
| fmt | GPT-SoVITS-cpp fmt submodule `f10b6dd8166d1b49e1b2e7511ceae063bb24edf7` | [LICENSE.txt](licenses/cpp/fmt/LICENSE.txt) |
| cpp-pinyin | GPT-SoVITS-cpp cpp-pinyin submodule `fbaefc65818ed27c23c67678498f677b01c5097c` | [LICENSE.txt](licenses/cpp/cpp-pinyin/LICENSE.txt) |
| json | GPT-SoVITS-cpp json submodule `fbec662afab55019654e471b65a846a47a696722` | [LICENSE.MIT.txt](licenses/cpp/json/LICENSE.MIT.txt) |
| boost | GPT-SoVITS-cpp boost-cmake submodule `b4542945397659170803986bfcc205721c624dd6`; Boost 1.0 text copied from its vendored msgpack dependency | [LICENSE_1_0.txt](licenses/cpp/boost/LICENSE_1_0.txt) |
| tokenizers-cpp | GPT-SoVITS-cpp tokenizers-cpp submodule `e450f2523078280cc950c9fa4d6bb9267282775e` | [LICENSE.txt](licenses/cpp/tokenizers-cpp/LICENSE.txt) |
| sentencepiece | vendored in tokenizers-cpp submodule `e450f2523078280cc950c9fa4d6bb9267282775e` | [LICENSE.txt](licenses/cpp/sentencepiece/LICENSE.txt), [LICENSE.txt](licenses/cpp/sentencepiece/third_party/protobuf-lite/LICENSE.txt), [LICENSE.txt](licenses/cpp/sentencepiece/third_party/esaxx/LICENSE.txt), [LICENSE.txt](licenses/cpp/sentencepiece/third_party/darts_clone/LICENSE.txt), [LICENSE.txt](licenses/cpp/sentencepiece/third_party/absl/LICENSE.txt) |
| msgpack | vendored in tokenizers-cpp submodule `e450f2523078280cc950c9fa4d6bb9267282775e` | [COPYING.txt](licenses/cpp/msgpack/COPYING.txt), [NOTICE.txt](licenses/cpp/msgpack/NOTICE.txt), [LICENSE_1_0.txt](licenses/cpp/msgpack/LICENSE_1_0.txt) |
| SRELL | GPT-SoVITS-cpp SRELL submodule `3cd3f74357a8ecdd82f21eb9bad9ae399961e0c1` | [LICENSE.txt](licenses/cpp/SRELL/LICENSE.txt) |
| xsimd | GPT-SoVITS-cpp xsimd submodule `6842624fc8adafd7168a999e7150b384411da448` | [LICENSE.txt](licenses/cpp/xsimd/LICENSE.txt) |
| xtensor | GPT-SoVITS-cpp xtensor submodule `ae52796961d03e7a3d754d72713be5098ce467b9` | [LICENSE.txt](licenses/cpp/xtensor/LICENSE.txt) |
| xtensor-blas | GPT-SoVITS-cpp xtensor-blas submodule `5dcbab5f41636a678a9ec715a058fbe6501aba77` | [LICENSE.txt](licenses/cpp/xtensor-blas/LICENSE.txt), [LICENSE.txt](licenses/cpp/xtensor-blas/include/xflens/cxxblas/LICENSE.txt), [LICENSE.txt](licenses/cpp/xtensor-blas/include/xflens/cxxlapack/LICENSE.txt) |
| xtl | GPT-SoVITS-cpp xtl submodule `d11fb6b5f4c417025124ed2c62175284846a1914` | [LICENSE.txt](licenses/cpp/xtl/LICENSE.txt) |
| utfcpp | GPT-SoVITS-cpp utfcpp submodule `70795627871251f6c57f952e667303a2381c2fc5` | [LICENSE.txt](licenses/cpp/utfcpp/LICENSE.txt) |
| cld2-cmake | GPT-SoVITS-cpp cld2-cmake submodule `0d8b95d739b8175a6e0bb06746e9d4ed7258197a` | [LICENSE.txt](licenses/cpp/cld2-cmake/LICENSE.txt) |
| cppjieba | GPT-SoVITS-cpp cppjieba submodule `16a3ec1adcbddfe9c24206fdbc6934b440b5cda9` | [LICENSE.txt](licenses/cpp/cppjieba/LICENSE.txt) |
| cpptrace | GPT-SoVITS-cpp cpptrace submodule `4b94ca34726152c34e4fb20a05d2c6b381344dce` | [LICENSE.txt](licenses/cpp/cpptrace/LICENSE.txt) |
| open_jtalk | GPT-SoVITS-cpp pinned source tree | [COPYING.txt](licenses/cpp/open_jtalk/COPYING.txt), [COPYING.txt](licenses/cpp/open_jtalk/mecab/COPYING.txt) |
| hts_engine | GPT-SoVITS-cpp pinned source tree | [COPYING.txt](licenses/cpp/hts_engine/COPYING.txt) |

The inference result owns float32 PCM in memory. The native build excludes
audio file codecs, reference-audio preparation, and a second resampler;
neither libsndfile nor libsamplerate is linked. RedLotus performs
standard-rate conversion through its existing SoXR interface.

## Rust crates compiled for the Windows x64 tokenizer target

The local tokenizers-c wrapper is part of the pinned tokenizers-cpp source.
The following crates appear in the Release target fingerprints and in
that source's Cargo.lock. Entries in Cargo.lock with no matching Release
fingerprint are not included in this target inventory. The fingerprint
inventory may include compile-time crates that contribute no binary code.

| Crate | Declared license | Cargo.lock SHA256 | License copies |
| --- | --- | --- | --- |
| [ahash 0.8.12](https://crates.io/crates/ahash/0.8.12) | `MIT OR Apache-2.0` | `5a15f179cd60c4584b8a8c596927aadc462e27f2ca70c04e0071964a73ba7a75` | [LICENSE-APACHE.txt](licenses/rust/ahash-0.8.12/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/ahash-0.8.12/LICENSE-MIT.txt) |
| [aho-corasick 1.1.5](https://crates.io/crates/aho-corasick/1.1.5) | `Unlicense OR MIT` | `c982642fa9e8606056828ee9a8505737230110bb1099153c79efe865c59d12ba` | [COPYING.txt](licenses/rust/aho-corasick-1.1.5/COPYING.txt), [LICENSE-MIT.txt](licenses/rust/aho-corasick-1.1.5/LICENSE-MIT.txt) |
| [base64 0.13.1](https://crates.io/crates/base64/0.13.1) | `MIT/Apache-2.0` | `9e1b586273c5702936fe7b7d6896644d8be71e6314cfe09d3167c95f712589e8` | [LICENSE-APACHE.txt](licenses/rust/base64-0.13.1/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/base64-0.13.1/LICENSE-MIT.txt) |
| [bitflags 2.13.2](https://crates.io/crates/bitflags/2.13.2) | `MIT OR Apache-2.0` | `3ded4057c258ba199e2d26386d3af3780957ecaee6c4ef4041c6b4b8b97c0b06` | [LICENSE-APACHE.txt](licenses/rust/bitflags-2.13.2/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/bitflags-2.13.2/LICENSE-MIT.txt) |
| [castaway 0.2.4](https://crates.io/crates/castaway/0.2.4) | `MIT` | `dec551ab6e7578819132c713a93c022a05d60159dc86e7a7050223577484c55a` | [LICENSE.txt](licenses/rust/castaway-0.2.4/LICENSE.txt) |
| [cfg-if 1.0.5](https://crates.io/crates/cfg-if/1.0.5) | `MIT OR Apache-2.0` | `4e7648175b45a9a48536d676f68d918270699102aa8dab5496df06904c914600` | [LICENSE-APACHE.txt](licenses/rust/cfg-if-1.0.5/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/cfg-if-1.0.5/LICENSE-MIT.txt) |
| [compact_str 0.9.1](https://crates.io/crates/compact_str/0.9.1) | `MIT` | `9dfdd1c2274d9aa354115b09dc9a901d6c5576818cdf70d14cae2bdb47df00ab` | [LICENSE.txt](licenses/rust/compact_str-0.9.1/LICENSE.txt) |
| [crossbeam-deque 0.8.8](https://crates.io/crates/crossbeam-deque/0.8.8) | `MIT OR Apache-2.0` | `622f3fc73690be383c7214310406f28a90e6edeadc3cea882f9d71e495b9711a` | [LICENSE-APACHE.txt](licenses/rust/crossbeam-deque-0.8.8/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/crossbeam-deque-0.8.8/LICENSE-MIT.txt) |
| [crossbeam-epoch 0.9.21](https://crates.io/crates/crossbeam-epoch/0.9.21) | `MIT OR Apache-2.0` | `dc74980687109a3b14c72fd458107bf0baa1da1a1a805e178d15501ba9b86d9d` | [LICENSE-APACHE.txt](licenses/rust/crossbeam-epoch-0.9.21/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/crossbeam-epoch-0.9.21/LICENSE-MIT.txt) |
| [crossbeam-utils 0.8.23](https://crates.io/crates/crossbeam-utils/0.8.23) | `MIT OR Apache-2.0` | `a31eee39dddec8330830986fcd7625edb5a24ec90ea038215273bbc3adb08ac6` | [LICENSE-APACHE.txt](licenses/rust/crossbeam-utils-0.8.23/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/crossbeam-utils-0.8.23/LICENSE-MIT.txt) |
| [dary_heap 0.3.9](https://crates.io/crates/dary_heap/0.3.9) | `MIT OR Apache-2.0` | `8b1e3a325bc115f096c8b77bbf027a7c2592230e70be2d985be950d3d5e60ebe` | [LICENSE-APACHE.txt](licenses/rust/dary_heap-0.3.9/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/dary_heap-0.3.9/LICENSE-MIT.txt) |
| [derive_builder 0.20.2](https://crates.io/crates/derive_builder/0.20.2) | `MIT OR Apache-2.0` | `507dfb09ea8b7fa618fcf76e953f4f5e192547945816d5358edffe39f6f94947` | [LICENSE-APACHE.txt](licenses/rust/derive_builder-0.20.2/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/derive_builder-0.20.2/LICENSE-MIT.txt) |
| [either 1.18.0](https://crates.io/crates/either/1.18.0) | `MIT OR Apache-2.0` | `252afb9ae5eaa683babdc6a068b3f5726eb19e05070c731f9b2a23a7c3e8ed34` | [LICENSE-APACHE.txt](licenses/rust/either-1.18.0/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/either-1.18.0/LICENSE-MIT.txt) |
| [esaxx-rs 0.1.10](https://crates.io/crates/esaxx-rs/0.1.10) | `Apache-2.0` | `d817e038c30374a4bcb22f94d0a8a0e216958d4c3dcde369b1439fec4bdda6e6` | [LICENSE.txt](licenses/rust/esaxx-rs-0.1.10/LICENSE.txt) |
| [getrandom 0.3.4](https://crates.io/crates/getrandom/0.3.4) | `MIT OR Apache-2.0` | `899def5c37c4fd7b2664648c28120ecec138e4d395b459e5ca34f9cce2dd77fd` | [LICENSE-APACHE.txt](licenses/rust/getrandom-0.3.4/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/getrandom-0.3.4/LICENSE-MIT.txt) |
| [itertools 0.14.0](https://crates.io/crates/itertools/0.14.0) | `MIT OR Apache-2.0` | `2b192c782037fadd9cfa75548310488aabdbf3d2da73885b31bd0abd03351285` | [LICENSE-APACHE.txt](licenses/rust/itertools-0.14.0/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/itertools-0.14.0/LICENSE-MIT.txt) |
| [itoa 1.0.18](https://crates.io/crates/itoa/1.0.18) | `MIT OR Apache-2.0` | `8f42a60cbdf9a97f5d2305f08a87dc4e09308d1276d28c869c684d7777685682` | [LICENSE-APACHE.txt](licenses/rust/itoa-1.0.18/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/itoa-1.0.18/LICENSE-MIT.txt) |
| [libc 0.2.189](https://crates.io/crates/libc/0.2.189) | `MIT OR Apache-2.0` | `3eaf3ede3fee6db1a4c2ee091bf8a8b4dccdc6d17f656fb07896ee72867612f2` | [LICENSE-APACHE.txt](licenses/rust/libc-0.2.189/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/libc-0.2.189/LICENSE-MIT.txt) |
| [log 0.4.34](https://crates.io/crates/log/0.4.34) | `MIT OR Apache-2.0` | `f9f8bd3e56ce4dfc153cf470fffbfa98c7620958b312ca5c3a4b8d5181fd13c6` | [LICENSE-APACHE.txt](licenses/rust/log-0.4.34/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/log-0.4.34/LICENSE-MIT.txt) |
| [macro_rules_attribute 0.2.3](https://crates.io/crates/macro_rules_attribute/0.2.3) | `Apache-2.0 OR MIT OR Zlib` | `b3ae8f6d608c795738406608304d30a2dfbdc8e58e44f7ba43236da5208ded3c` | [LICENSE-APACHE.txt](licenses/rust/macro_rules_attribute-0.2.3/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/macro_rules_attribute-0.2.3/LICENSE-MIT.txt), [LICENSE-ZLIB.txt](licenses/rust/macro_rules_attribute-0.2.3/LICENSE-ZLIB.txt) |
| [memchr 2.8.3](https://crates.io/crates/memchr/2.8.3) | `Unlicense OR MIT` | `cf8baf1c55e62ffcace7a9f06f4bd9cd3f0c4beb022d3b367256b91b87513d98` | [COPYING.txt](licenses/rust/memchr-2.8.3/COPYING.txt), [LICENSE-MIT.txt](licenses/rust/memchr-2.8.3/LICENSE-MIT.txt) |
| [minimal-lexical 0.2.1](https://crates.io/crates/minimal-lexical/0.2.1) | `MIT/Apache-2.0` | `68354c5c6bd36d73ff3feceb05efa59b6acb7626617f4962be322a825e61f79a` | [LICENSE-APACHE.txt](licenses/rust/minimal-lexical-0.2.1/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/minimal-lexical-0.2.1/LICENSE-MIT.txt), [LICENSE.md](licenses/rust/minimal-lexical-0.2.1/LICENSE.md) |
| [monostate 0.1.18](https://crates.io/crates/monostate/0.1.18) | `MIT OR Apache-2.0` | `3341a273f6c9d5bef1908f17b7267bbab0e95c9bf69a0d4dcf8e9e1b2c76ef67` | [LICENSE-APACHE.txt](licenses/rust/monostate-0.1.18/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/monostate-0.1.18/LICENSE-MIT.txt) |
| [nom 7.1.3](https://crates.io/crates/nom/7.1.3) | `MIT` | `d273983c5a657a70a3e8f2a01329822f3b8c8172b73826411a55751e404a0a4a` | [LICENSE.txt](licenses/rust/nom-7.1.3/LICENSE.txt) |
| [once_cell 1.21.4](https://crates.io/crates/once_cell/1.21.4) | `MIT OR Apache-2.0` | `9f7c3e4beb33f85d45ae3e3a1792185706c8e16d043238c593331cc7cd313b50` | [LICENSE-APACHE.txt](licenses/rust/once_cell-1.21.4/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/once_cell-1.21.4/LICENSE-MIT.txt) |
| [onig 6.5.3](https://crates.io/crates/onig/6.5.3) | `MIT` | `0cc3cbf698f9438986c11a880c90a6d04b9de27575afd28bbf45b154b6c709e2` | [LICENSE.md](licenses/rust/onig-6.5.3/LICENSE.md) |
| [onig_sys 69.9.3](https://crates.io/crates/onig_sys/69.9.3) | `MIT` | `1e68317604e77e53b85896388e1a803c1d21b74c899ec9e5e1112db90735edd7` | [LICENSE.md](licenses/rust/onig_sys-69.9.3/LICENSE.md), [COPYING.txt](licenses/rust/onig_sys-69.9.3/oniguruma/COPYING.txt) |
| [ppv-lite86 0.2.21](https://crates.io/crates/ppv-lite86/0.2.21) | `MIT OR Apache-2.0` | `85eae3c4ed2f50dcfe72643da4befc30deadb458a9b590d720cde2f2b1e97da9` | [LICENSE-APACHE.txt](licenses/rust/ppv-lite86-0.2.21/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/ppv-lite86-0.2.21/LICENSE-MIT.txt) |
| [rand 0.9.5](https://crates.io/crates/rand/0.9.5) | `MIT OR Apache-2.0` | `b9ef1d0d795eb7d84685bca4f72f3649f064e6641543d3a8c415898726a57b41` | [LICENSE-APACHE.txt](licenses/rust/rand-0.9.5/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/rand-0.9.5/LICENSE-MIT.txt) |
| [rand_chacha 0.9.0](https://crates.io/crates/rand_chacha/0.9.0) | `MIT OR Apache-2.0` | `d3022b5f1df60f26e1ffddd6c66e8aa15de382ae63b3a0c1bfc0e4d3e3f325cb` | [LICENSE-APACHE.txt](licenses/rust/rand_chacha-0.9.0/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/rand_chacha-0.9.0/LICENSE-MIT.txt) |
| [rand_core 0.9.5](https://crates.io/crates/rand_core/0.9.5) | `MIT OR Apache-2.0` | `76afc826de14238e6e8c374ddcc1fa19e374fd8dd986b0d2af0d02377261d83c` | [LICENSE-APACHE.txt](licenses/rust/rand_core-0.9.5/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/rand_core-0.9.5/LICENSE-MIT.txt) |
| [rayon 1.12.0](https://crates.io/crates/rayon/1.12.0) | `MIT OR Apache-2.0` | `fb39b166781f92d482534ef4b4b1b2568f42613b53e5b6c160e24cfbfa30926d` | [LICENSE-APACHE.txt](licenses/rust/rayon-1.12.0/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/rayon-1.12.0/LICENSE-MIT.txt) |
| [rayon-cond 0.4.0](https://crates.io/crates/rayon-cond/0.4.0) | `Apache-2.0/MIT` | `2964d0cf57a3e7a06e8183d14a8b527195c706b7983549cd5462d5aa3747438f` | [LICENSE-APACHE.txt](licenses/rust/rayon-cond-0.4.0/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/rayon-cond-0.4.0/LICENSE-MIT.txt) |
| [rayon-core 1.13.0](https://crates.io/crates/rayon-core/1.13.0) | `MIT OR Apache-2.0` | `22e18b0f0062d30d4230b2e85ff77fdfe4326feb054b9783a3460d8435c8ab91` | [LICENSE-APACHE.txt](licenses/rust/rayon-core-1.13.0/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/rayon-core-1.13.0/LICENSE-MIT.txt) |
| [regex 1.13.1](https://crates.io/crates/regex/1.13.1) | `MIT OR Apache-2.0` | `f020237b6c8eed93db2e2cb53c00c60a8e1bc73da7d073199a1180401450218d` | [LICENSE-APACHE.txt](licenses/rust/regex-1.13.1/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/regex-1.13.1/LICENSE-MIT.txt) |
| [regex-automata 0.4.18](https://crates.io/crates/regex-automata/0.4.18) | `MIT OR Apache-2.0` | `ad8553b9b26413251cbf30e620595c7a41b3887f03da04579c0e6b0d6a06b4b2` | [LICENSE-APACHE.txt](licenses/rust/regex-automata-0.4.18/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/regex-automata-0.4.18/LICENSE-MIT.txt) |
| [regex-syntax 0.8.11](https://crates.io/crates/regex-syntax/0.8.11) | `MIT OR Apache-2.0` | `d6f6ff9a378485b298a5286656da665ba74413d36db0979633275d2e708145d4` | [LICENSE-APACHE.txt](licenses/rust/regex-syntax-0.8.11/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/regex-syntax-0.8.11/LICENSE-MIT.txt) |
| [ryu 1.0.23](https://crates.io/crates/ryu/1.0.23) | `Apache-2.0 OR BSL-1.0` | `9774ba4a74de5f7b1c1451ed6cd5285a32eddb5cccb8cc655a4e50009e06477f` | [LICENSE-APACHE.txt](licenses/rust/ryu-1.0.23/LICENSE-APACHE.txt), [LICENSE-BOOST.txt](licenses/rust/ryu-1.0.23/LICENSE-BOOST.txt) |
| [serde 1.0.229](https://crates.io/crates/serde/1.0.229) | `MIT OR Apache-2.0` | `4148590afebada386688f18773da617792bf2ef03ffc1e4cbd2b1d45b023e0ba` | [LICENSE-APACHE.txt](licenses/rust/serde-1.0.229/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/serde-1.0.229/LICENSE-MIT.txt) |
| [serde_core 1.0.229](https://crates.io/crates/serde_core/1.0.229) | `MIT OR Apache-2.0` | `67dca2c9c51e58a4791a4b1ed58308b39c64224d349a935ab5039aa360942a48` | [LICENSE-APACHE.txt](licenses/rust/serde_core-1.0.229/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/serde_core-1.0.229/LICENSE-MIT.txt) |
| [serde_json 1.0.151](https://crates.io/crates/serde_json/1.0.151) | `MIT OR Apache-2.0` | `c841b55ecdae098c80dcae9cf767f6f8a0c2cdb3416bbef72181df4d0fe73f14` | [LICENSE-APACHE.txt](licenses/rust/serde_json-1.0.151/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/serde_json-1.0.151/LICENSE-MIT.txt) |
| [smallvec 1.16.2](https://crates.io/crates/smallvec/1.16.2) | `MIT OR Apache-2.0` | `f9395f0f0eee849a9b707b2f06bb92a6a422090e2123bb2ef8e87a0e61892a8e` | [LICENSE-APACHE.txt](licenses/rust/smallvec-1.16.2/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/smallvec-1.16.2/LICENSE-MIT.txt) |
| [spm_precompiled 0.1.4](https://crates.io/crates/spm_precompiled/0.1.4) | `Apache-2.0` | `5851699c4033c63636f7ea4cf7b7c1f1bf06d0cc03cfb42e711de5a5c46cf326` | [LICENSE.txt](licenses/rust/spm_precompiled-0.1.4/LICENSE.txt) |
| [static_assertions 1.1.0](https://crates.io/crates/static_assertions/1.1.0) | `MIT OR Apache-2.0` | `a2eb9349b6444b326872e140eb1cf5e7c522154d69e7a0ffb0fb81c06b37543f` | [LICENSE-APACHE.txt](licenses/rust/static_assertions-1.1.0/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/static_assertions-1.1.0/LICENSE-MIT.txt) |
| [thiserror 2.0.21](https://crates.io/crates/thiserror/2.0.21) | `MIT OR Apache-2.0` | `09e52cb86a36cede5cb101bf8908837b3e4c6e5e59fe7fd85c23fb56200d189e` | [LICENSE-APACHE.txt](licenses/rust/thiserror-2.0.21/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/thiserror-2.0.21/LICENSE-MIT.txt) |
| [tokenizers 0.21.4](https://crates.io/crates/tokenizers/0.21.4) | `Apache-2.0` | `a620b996116a59e184c2fa2dfd8251ea34a36d0a514758c6f966386bd2e03476` | [LICENSE.txt](licenses/rust/tokenizers-0.21.4/LICENSE.txt) |
| [unicode-normalization-alignments 0.1.12](https://crates.io/crates/unicode-normalization-alignments/0.1.12) | `MIT/Apache-2.0` | `43f613e4fa046e69818dd287fdc4bc78175ff20331479dab6e1b0f98d57062de` | [LICENSE-APACHE.txt](licenses/rust/unicode-normalization-alignments-0.1.12/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/unicode-normalization-alignments-0.1.12/LICENSE-MIT.txt) |
| [unicode-segmentation 1.13.3](https://crates.io/crates/unicode-segmentation/1.13.3) | `MIT OR Apache-2.0` | `c6f5d3c3b1bf09027a88a6bc961fc00497d651009560b5463668dc81b0fa87a8` | [LICENSE-APACHE.txt](licenses/rust/unicode-segmentation-1.13.3/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/unicode-segmentation-1.13.3/LICENSE-MIT.txt) |
| [unicode_categories 0.1.1](https://crates.io/crates/unicode_categories/0.1.1) | `MIT OR Apache-2.0` | `39ec24b3121d976906ece63c9daad25b85969647682eee313cb5779fdd69e14e` | [LICENSE-APACHE.txt](licenses/rust/unicode_categories-0.1.1/LICENSE-APACHE.txt), [LICENSE-MIT.txt](licenses/rust/unicode_categories-0.1.1/LICENSE-MIT.txt) |
| [zerocopy 0.8.59](https://crates.io/crates/zerocopy/0.8.59) | `BSD-2-Clause OR Apache-2.0 OR MIT` | `6df92bf3d9227be3d53173901ddbffac2babc27ae50f397776ffd6dc33f800cb` | [LICENSE-APACHE.txt](licenses/rust/zerocopy-0.8.59/LICENSE-APACHE.txt), [LICENSE-BSD.txt](licenses/rust/zerocopy-0.8.59/LICENSE-BSD.txt), [LICENSE-MIT.txt](licenses/rust/zerocopy-0.8.59/LICENSE-MIT.txt) |
| [zmij 1.0.23](https://crates.io/crates/zmij/1.0.23) | `MIT` | `29666d0abbfad1e3dc4dcf6144730dd3a3ab225bbbdac83319345b1b44ccfc1b` | [LICENSE-MIT.txt](licenses/rust/zmij-1.0.23/LICENSE-MIT.txt) |

The worker loads ONNX Runtime from the installed `sherpa-onnx` package
at runtime. It does not carry another ONNX Runtime DLL. That separate
runtime dependency and the downloadable Mambo model/voice resources
need their own distribution notices and review; this file does not
claim that the upstream software licenses grant rights to every voice
or dictionary in the model archive.
